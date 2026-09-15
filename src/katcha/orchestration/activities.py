from __future__ import annotations

import shutil
import uuid
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
            source.source_metadata = media.source_metadata
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
