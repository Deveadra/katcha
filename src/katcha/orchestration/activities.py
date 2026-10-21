from __future__ import annotations

import logging
import shutil
import uuid
from contextlib import suppress
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlsplit

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from temporalio import activity
from temporalio.exceptions import ApplicationError

from katcha.db import session_scope
from katcha.domain import ClipStatus, SourceStatus
from katcha.integrations.direct_media import is_direct_media_url, stream_direct_media
from katcha.integrations.download import canonicalize_url, detect_platform, download
from katcha.integrations.object_streaming import (
    MultipartObjectWriter,
    inspect_stored_media,
    verify_stored_sha256,
)
from katcha.integrations.storage import ObjectStore
from katcha.models import Clip, DomainEvent, SourceItem
from katcha.ops.direct_promotion_provenance import PROOF_KEY, record_verified_direct_promotion
from katcha.orchestration.failure_details import MediaPreparationError, failure_details
from katcha.orchestration.ingest_recovery import RECOVERY_KEY

logger = logging.getLogger(__name__)


def _extract_video_shape(
    metadata: dict[str, object],
) -> tuple[Decimal | None, int | None, int | None]:
    format_info = metadata.get("format") if isinstance(metadata, dict) else None
    duration: Decimal | None = None
    if isinstance(format_info, dict) and format_info.get("duration") is not None:
        duration = Decimal(str(format_info["duration"]))

    width: int | None = None
    height: int | None = None
    streams = metadata.get("streams") if isinstance(metadata, dict) else None
    if isinstance(streams, list):
        for stream in streams:
            if isinstance(stream, dict) and stream.get("codec_type") == "video":
                if stream.get("width") is not None:
                    width = int(stream["width"])
                if stream.get("height") is not None:
                    height = int(stream["height"])
                break
    return duration, width, height


@activity.defn
def ingest_source(source_id: str) -> dict[str, str | bool]:
    try:
        return _ingest_source(source_id)
    except Exception as exc:
        detail = failure_details(exc)
        # Retain each attempt before Temporal wraps it. The final workflow
        # failure still owns FAILED status; a retry may subsequently succeed.
        try:
            with session_scope() as session:
                source = session.get(SourceItem, uuid.UUID(source_id))
                if source is not None and source.status != SourceStatus.READY.value:
                    source.error = detail["message"]
                    source.source_metadata = {
                        **dict(source.source_metadata or {}), "ingestion_diagnostic": detail,
                    }
                    session.add(DomainEvent(
                        aggregate_type="source", aggregate_id=str(source.id),
                        event_type="source.ingest_attempt_failed",
                        payload={"source_item_id": str(source.id), "diagnostic": detail},
                    ))
        except Exception as diagnostic_error:
            logger.warning(
                "Could not persist ingestion diagnostic source=%s failure_type=%s",
                source_id, type(diagnostic_error).__name__,
            )
        raise ApplicationError(
            detail["message"],
            {key: detail[key] for key in ("stage", "code", "retryable", "recovery")},
            type="MediaIngestionFailure", non_retryable=detail["retryable"] is False,
        ) from exc


def _ingest_source(source_id: str) -> dict[str, str | bool]:
    source_uuid = uuid.UUID(source_id)
    with session_scope() as session:
        source = session.get(SourceItem, source_uuid)
        if source is None:
            raise ValueError(f"source not found: {source_id}")
        if source.status == SourceStatus.READY.value and source.clip_id is not None:
            return {"source_id": str(source.id), "clip_id": str(source.clip_id), "deduped": True}
        from katcha.config import get_settings

        if (
            (source.source_metadata or {}).get("remote_acquisition_job_id")
            or (get_settings().remote_acquisition_enabled
                and get_settings().remote_acquisition_auto_dispatch_enabled)
        ):
            raise ApplicationError(
                "Remote acquisition is still pending; wait for its verified handoff",
                type="RemoteAcquisitionPending", non_retryable=True,
            )
        if (source.source_metadata or {}).get(PROOF_KEY) is not None:
            # A previous attempt may have reached raw/ before losing its
            # SourceItem->Clip transaction. Never redownload or overwrite its
            # provenance evidence; reconcile it using the operator procedure.
            raise ApplicationError(
                "Verified direct-media promotion requires evidence reconciliation "
                "before any new ingest attempt",
                type="DirectMediaRecoveryRequired",
                non_retryable=True,
            )
        source.status = SourceStatus.INGESTING.value
        source.error = None
        source_url = source.source_url

    canonical = canonicalize_url(source_url)
    if detect_platform(canonical) == "generic" and is_direct_media_url(canonical):
        return _ingest_direct_source(source_uuid, canonical)

    media = download(source_url)
    stage = "object storage setup"
    try:
        store = ObjectStore()
        store.ensure_bucket()
        duration, width, height = _extract_video_shape(media.media_metadata)
        storage_key = store.raw_key(media.sha256, media.extension)

        stage = "clip deduplication lookup"
        with session_scope() as session:
            existing = session.scalar(select(Clip).where(Clip.sha256 == media.sha256))
            deduped = existing is not None
            if deduped and existing is not None:
                storage_key = existing.storage_key

        stage = "object storage upload"
        if not deduped and not store.exists(storage_key):
            store.put_file(media.path, storage_key)

        stage = "clip persistence"
        with session_scope() as session:
            stmt = (
                pg_insert(Clip)
                .values(
                    sha256=media.sha256,
                    storage_key=storage_key,
                    extension=media.extension,
                    size_bytes=media.size_bytes,
                    duration_seconds=duration,
                    width=width,
                    height=height,
                    status=ClipStatus.INGESTED.value,
                    media_metadata=media.media_metadata,
                )
                .on_conflict_do_nothing(index_elements=[Clip.sha256])
                .returning(Clip.id)
            )
            new_clip_id = session.execute(stmt).scalar_one_or_none()
            if new_clip_id is None:
                clip = session.scalar(select(Clip).where(Clip.sha256 == media.sha256))
                if clip is None:
                    raise RuntimeError("clip reconciliation failed after SHA conflict")
                deduped = True
            else:
                clip = session.get(Clip, new_clip_id)
                if clip is None:
                    raise RuntimeError("new clip could not be loaded")

            source = session.get(SourceItem, source_uuid)
            if source is None:
                raise RuntimeError(f"source disappeared during ingest: {source_id}")
            source.clip_id = clip.id
            source.status = SourceStatus.READY.value
            # Direct publisher files usually have no extractor title/creator.
            # Preserve the candidate's existing editorial provenance instead.
            if media.title is not None:
                source.title = media.title
            if media.creator is not None:
                source.creator = media.creator
            source.platform = media.platform
            source.canonical_url = media.canonical_url
            # Provider metadata must not erase (or replace) Katcha's channel and
            # acquisition lineage established before the download.
            source.source_metadata = {
                **dict(media.source_metadata or {}),
                **dict(source.source_metadata or {}),
            }
            source.error = None
            source.source_metadata = {
                k: v for k, v in source.source_metadata.items()
                if k not in {"ingestion_diagnostic", RECOVERY_KEY}
            }

            session.add(
                DomainEvent(
                    aggregate_type="source",
                    aggregate_id=str(source.id),
                    event_type="source.ingested",
                    payload={
                        "source_id": str(source.id),
                        "clip_id": str(clip.id),
                        "sha256": clip.sha256,
                        "deduped": deduped,
                    },
                )
            )
            return {"source_id": str(source.id), "clip_id": str(clip.id), "deduped": deduped}
    except Exception as exc:
        raise MediaPreparationError(stage, exc) from exc
    finally:
        shutil.rmtree(Path(media.path).parent, ignore_errors=True)


def _ingest_direct_source(source_uuid: uuid.UUID, canonical: str) -> dict[str, str | bool]:
    """Stream eligible publisher media to R2, then inspect and persist its real bytes."""
    stage = "direct object storage setup"
    store = ObjectStore()
    stage_key = ""
    writer: MultipartObjectWriter | None = None
    try:
        store.ensure_bucket()
        extension = urlsplit(canonical).path.rsplit(".", 1)[-1].lower()
        stage_key = f"staging/direct/{source_uuid}/{uuid.uuid4().hex}.{extension}"
        stage = "direct HTTPS multipart acquisition"
        writer = MultipartObjectWriter(store, stage_key, content_type="video/" + extension)
        transferred = stream_direct_media(canonical, writer.write)
        writer.finish()

        stage = "stored media SHA-256 verification"
        verify_stored_sha256(store, stage_key, transferred.sha256, transferred.size_bytes)
        stage = "stored media video inspection"
        metadata = inspect_stored_media(store, stage_key, transferred.size_bytes)
        duration, width, height = _extract_video_shape(metadata)

        stage = "clip deduplication lookup"
        with session_scope() as session:
            existing = session.scalar(select(Clip).where(Clip.sha256 == transferred.sha256))
            deduped = existing is not None
            storage_key = existing.storage_key if existing else store.raw_key(
                transferred.sha256, transferred.extension
            )

        # The verified source-to-sha claim is committed before the stage is
        # promoted into raw/. A crash after copying no longer loses ownership
        # evidence; a different retry may not overwrite an unresolved claim.
        stage = "durable direct-media provenance checkpoint"
        record_verified_direct_promotion(
            source_uuid,
            canonical_url=canonical,
            stage_key=stage_key,
            sha256=transferred.sha256,
            size_bytes=transferred.size_bytes,
            extension=transferred.extension,
            media_metadata=metadata,
        )

        stage = "content-addressed object reconciliation"
        raw_exists = not deduped and store.exists(storage_key)
        if not deduped and not raw_exists:
            store.move(stage_key, storage_key)
        elif raw_exists:
            # A raw/ key may survive a crash before Clip persistence. Never
            # assume that its filename proves the bytes are intact.
            stage = "existing raw object SHA-256 verification"
            verify_stored_sha256(
                store, storage_key, transferred.sha256, transferred.size_bytes
            )

        stage = "direct clip persistence"
        with session_scope() as session:
            stmt = (
                pg_insert(Clip)
                .values(
                    sha256=transferred.sha256,
                    storage_key=storage_key,
                    extension=transferred.extension,
                    size_bytes=transferred.size_bytes,
                    duration_seconds=duration,
                    width=width,
                    height=height,
                    status=ClipStatus.INGESTED.value,
                    media_metadata=metadata,
                )
                .on_conflict_do_nothing(index_elements=[Clip.sha256])
                .returning(Clip.id)
            )
            new_id = session.execute(stmt).scalar_one_or_none()
            if new_id is None:
                clip = session.scalar(select(Clip).where(Clip.sha256 == transferred.sha256))
                if clip is None:
                    raise RuntimeError("Direct clip reconciliation failed after SHA conflict")
                deduped = True
            else:
                clip = session.get(Clip, new_id)
                if clip is None:
                    raise RuntimeError("New direct clip was not persisted")

            source = session.get(SourceItem, source_uuid)
            if source is None:
                raise RuntimeError("Direct source disappeared during ingestion")
            source.clip_id = clip.id
            source.status = SourceStatus.READY.value
            source.platform = "generic"
            source.canonical_url = canonical
            source.error = None
            source.source_metadata = {
                **dict(source.source_metadata or {}),
                "acquisition_transport": "pinned_https_r2_multipart",
                "direct_media_host": transferred.final_host,
                "direct_media_redirects": transferred.redirects,
                "direct_media_content_type": transferred.content_type,
            }
            source.source_metadata.pop("ingestion_diagnostic", None)
            source.source_metadata.pop(RECOVERY_KEY, None)
            session.add(
                DomainEvent(
                    aggregate_type="source",
                    aggregate_id=str(source.id),
                    event_type="source.ingested",
                    payload={
                        "source_id": str(source.id),
                        "clip_id": str(clip.id),
                        "sha256": clip.sha256,
                        "deduped": deduped,
                        "acquisition_transport": "pinned_https_r2_multipart",
                    },
                )
            )
            return {"source_id": str(source.id), "clip_id": str(clip.id), "deduped": deduped}
    except Exception as exc:
        raise MediaPreparationError(stage, exc) from exc
    finally:
        if writer is not None:
            writer.abort()
        if stage_key:
            with suppress(Exception):
                if store.exists(stage_key):
                    store.delete(stage_key)


@activity.defn
def mark_source_failed(source_id: str, message: str) -> None:
    source_uuid = uuid.UUID(source_id)
    with session_scope() as session:
        source = session.get(SourceItem, source_uuid)
        if source is None:
            return
        source.status = SourceStatus.FAILED.value
        source.error = message[:8000]
        session.add(
            DomainEvent(
                aggregate_type="source",
                aggregate_id=str(source.id),
                event_type="source.ingest_failed",
                payload={"source_id": str(source.id), "error": source.error},
            )
        )


@activity.defn
def prepare_authorized_passthrough_activity(source_id: str) -> dict[str, object]:
    from katcha.services.trailer_passthrough import (
        prepare_authorized_passthrough_source,
    )

    return prepare_authorized_passthrough_source(uuid.UUID(source_id))


@activity.defn
async def enqueue_ingested_analysis_activity(clip_id: str) -> dict[str, str]:
    from katcha.orchestration.client import start_analysis_workflow
    from katcha.services.analysis import register_analysis

    run = register_analysis(uuid.UUID(clip_id))
    if run.status == "queued":
        await start_analysis_workflow(str(run.id), run.workflow_id)
    return {"analysis_run_id": str(run.id), "analysis_workflow_id": run.workflow_id}
