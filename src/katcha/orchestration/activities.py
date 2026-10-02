from __future__ import annotations

import shutil
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from temporalio import activity

from katcha.db import session_scope
from katcha.domain import ClipStatus, SourceStatus
from katcha.integrations.download import download
from katcha.integrations.storage import ObjectStore
from katcha.models import Clip, DomainEvent, SourceItem


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
    try:
        store = ObjectStore()
        store.ensure_bucket()
        duration, width, height = _extract_video_shape(media.media_metadata)
        storage_key = store.raw_key(media.sha256, media.extension)

        with session_scope() as session:
            existing = session.scalar(select(Clip).where(Clip.sha256 == media.sha256))
            deduped = existing is not None
            if deduped and existing is not None:
                storage_key = existing.storage_key

        if not deduped and not store.exists(storage_key):
            store.put_file(media.path, storage_key)

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


def _publication_time(value: object | None) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        candidate = str(value).strip()
        if not candidate:
            return None
        if candidate.endswith("Z"):
            candidate = candidate[:-1] + "+00:00"
        parsed = datetime.fromisoformat(candidate)
    if parsed.tzinfo is None:
        raise ValueError("publish_at must include a timezone")
    return parsed.astimezone(UTC)


@activity.defn
def prepare_authorized_passthrough_activity(source_id: str) -> dict[str, object]:
    """Turn an ingested authorized trailer into visible pre-upload work."""
    from katcha.intelligence_models import ChannelProfile
    from katcha.services.packaging_generation import (
        AmbiguousPackagingGeneration,
        PackagingGenerationUnavailable,
        generate_packaging_candidates,
    )
    from katcha.services.productions import register_source_passthrough_production
    from katcha.services.publications import register_publication

    source_uuid = uuid.UUID(source_id)
    with session_scope() as session:
        source = session.get(SourceItem, source_uuid)
        if source is None:
            raise ValueError(f"source not found: {source_id}")
        if source.clip_id is None or source.status != SourceStatus.READY.value:
            return {
                "source_id": source_id,
                "action": "skipped",
                "reason": "source is not ready",
            }
        metadata = dict(source.source_metadata or {})
        discovery = dict(metadata.get("discovery_metadata") or {})
        payload = dict(discovery.get("intelligence_payload") or {})
        if not (
            bool(discovery.get("operator_authorized"))
            and discovery.get("authorization_scope") == "official_trailer_repost"
            and discovery.get("official_source_verified")
            and payload.get("production_intent") == "source_passthrough"
        ):
            return {
                "source_id": source_id,
                "clip_id": str(source.clip_id),
                "action": "skipped",
                "reason": "source is not an authorized official-trailer passthrough",
            }
        channel_value = metadata.get("channel_profile_id")
        if not channel_value:
            raise ValueError("authorized trailer source has no channel_profile_id")
        channel_profile_id = uuid.UUID(str(channel_value))
        profile = session.get(ChannelProfile, channel_profile_id)
        if profile is None or profile.status != "active":
            raise ValueError("authorized trailer source channel profile is not active")
        youtube_connection_id = profile.youtube_connection_id
        clip_id = source.clip_id
        title = (
            str(metadata.get("intelligence_title") or "").strip()
            or str(source.title or "").strip()
            or "Untitled official trailer"
        )
        summary = str(metadata.get("intelligence_summary") or "").strip()
        tags = [
            str(value).strip()
            for value in (discovery.get("intelligence_tags") or [])
            if str(value).strip()
        ][:50]
        intelligence_record_id = str(
            metadata.get("intelligence_record_id")
            or discovery.get("intelligence_record_id")
            or ""
        ).strip()
        publish_at = _publication_time(payload.get("publish_at"))
        privacy_status = str(payload.get("privacy_status") or "public").strip().lower()
        if privacy_status not in {"private", "unlisted", "public"}:
            privacy_status = "public"
        hold_for_packaging = bool(payload.get("preupload_packaging_required", True))
        candidate_count = int(payload.get("packaging_candidate_count") or 3)
        candidate_count = min(5, max(2, candidate_count))

    production = register_source_passthrough_production(
        clip_id,
        channel_profile_id=channel_profile_id,
        idempotency_key=(
            f"handoff-{intelligence_record_id}"
            if intelligence_record_id
            else f"source-{source_uuid}"
        ),
        actor="intelligence_handoff",
    )
    publication = register_publication(
        production.id,
        youtube_connection_id=youtube_connection_id,
        title=title,
        description=summary,
        tags=tags,
        privacy_status=privacy_status,
        publish_at=publish_at,
        notify_subscribers=bool(payload.get("notify_subscribers", False)),
        made_for_kids=bool(payload.get("made_for_kids", False)),
        contains_synthetic_media=False,
        hold_for_packaging=hold_for_packaging,
    )

    packaging_status = "not_requested"
    packaging_generation_id: str | None = None
    if hold_for_packaging:
        generation_key = (
            f"handoff-{intelligence_record_id}-seo-v1"
            if intelligence_record_id
            else f"handoff-source-{source_uuid}-seo-v1"
        )
        try:
            generated = generate_packaging_candidates(
                publication.id,
                generation_key=generation_key,
                candidate_count=candidate_count,
            )
            packaging_generation_id = str(generated.generation.id)
            packaging_status = generated.generation.status
        except (
            PackagingGenerationUnavailable,
            AmbiguousPackagingGeneration,
            ValueError,
        ) as exc:
            packaging_status = "needs_attention"
            with session_scope() as session:
                session.add(
                    DomainEvent(
                        aggregate_type="publication",
                        aggregate_id=str(publication.id),
                        event_type="publication.packaging_generation_failed",
                        payload={
                            "publication_id": str(publication.id),
                            "source_id": source_id,
                            "intelligence_record_id": intelligence_record_id or None,
                            "error": str(exc)[:4000],
                        },
                    )
                )

    return {
        "source_id": source_id,
        "clip_id": str(clip_id),
        "production_id": str(production.id),
        "publication_id": str(publication.id),
        "publication_stage": publication.stage,
        "publish_at": publication.publish_at.isoformat() if publication.publish_at else None,
        "packaging_status": packaging_status,
        "packaging_generation_id": packaging_generation_id,
        "action": "prepared",
    }


@activity.defn
async def enqueue_ingested_analysis_activity(clip_id: str) -> dict[str, str]:
    from katcha.orchestration.client import start_analysis_workflow
    from katcha.services.analysis import register_analysis

    run = register_analysis(uuid.UUID(clip_id))
    if run.status == "queued":
        await start_analysis_workflow(str(run.id), run.workflow_id)
    return {"analysis_run_id": str(run.id), "analysis_workflow_id": run.workflow_id}
