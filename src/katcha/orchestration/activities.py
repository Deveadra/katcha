from __future__ import annotations

import logging
import shutil
import uuid
from decimal import Decimal
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from temporalio import activity
from temporalio.exceptions import ApplicationError

from katcha.db import session_scope
from katcha.domain import ClipStatus, SourceStatus
from katcha.integrations.download import download
from katcha.integrations.storage import ObjectStore
from katcha.models import Clip, DomainEvent, SourceItem
from katcha.orchestration.failure_details import MediaPreparationError, failure_details

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
            detail["message"], {"stage": detail["stage"]}, type="MediaIngestionFailure"
        ) from exc


def _ingest_source(source_id: str) -> dict[str, str | bool]:
    source_uuid = uuid.UUID(source_id)
    with session_scope() as session:
        source = session.get(SourceItem, source_uuid)
        if source is None:
            raise ValueError(f"source not found: {source_id}")
        if source.status == SourceStatus.READY.value and source.clip_id is not None:
            return {"source_id": str(source.id), "clip_id": str(source.clip_id), "deduped": True}
        source.status = SourceStatus.INGESTING.value
        source.error = None
        source_url = source.source_url

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
            source.title = media.title
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
                k: v for k, v in source.source_metadata.items() if k != "ingestion_diagnostic"
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
