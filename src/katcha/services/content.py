from __future__ import annotations

import hashlib
import shutil
import uuid
from pathlib import Path
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from katcha.config import get_settings
from katcha.content_models import ContentItem
from katcha.db import session_scope
from katcha.integrations.download import ffprobe, sha256_file
from katcha.integrations.storage import ObjectStore
from katcha.intelligence_models import ChannelProfile
from katcha.models import Clip, DomainEvent, SourceItem
from katcha.orchestration.activities import _extract_video_shape
from katcha.production_models import Production
from katcha.publishing_models import Publication
from katcha.services.channel_profiles import ensure_active_profile

CHUNK_BYTES = 8 * 1024 * 1024
MAX_FILE_BYTES = 4 * 1024 * 1024 * 1024


def _item(session: Session, channel_id: uuid.UUID, item_id: uuid.UUID) -> ContentItem:
    row = session.scalar(
        select(ContentItem)
        .where(ContentItem.id == item_id, ContentItem.channel_profile_id == channel_id)
        .with_for_update()
    )
    if row is None:
        raise ValueError("Content not found in this channel")
    return row


def create_content(
    channel_id: uuid.UUID,
    *,
    request_key: str,
    input_kind: str,
    title: str,
    filename: str | None = None,
    size_bytes: int | None = None,
    source_id: uuid.UUID | None = None,
    clip_id: uuid.UUID | None = None,
    details: dict[str, Any] | None = None,
) -> ContentItem:
    with session_scope() as session:
        ensure_active_profile(session, channel_id)
        existing = session.scalar(
            select(ContentItem).where(
                ContentItem.channel_profile_id == channel_id, ContentItem.request_key == request_key
            )
        )
        if existing:
            if (existing.input_kind, existing.filename, existing.size_bytes) != (
                input_kind,
                filename,
                size_bytes,
            ) or existing.details.get("fingerprint") != (details or {}).get("fingerprint"):
                raise ValueError("This request already belongs to a different input")
            session.expunge(existing)
            return existing
        row = ContentItem(
            channel_profile_id=channel_id,
            request_key=request_key,
            input_kind=input_kind,
            title=title,
            filename=filename,
            size_bytes=size_bytes,
            source_id=source_id,
            clip_id=clip_id,
            upload_offset=0,
            status="uploading" if input_kind == "file" else "received",
            details=details or {},
        )
        try:
            with session.begin_nested():
                session.add(row)
                session.flush()
        except IntegrityError:
            # Concurrent retries converge on the unique channel/request identity.
            row = session.scalar(
                select(ContentItem).where(
                    ContentItem.channel_profile_id == channel_id,
                    ContentItem.request_key == request_key,
                )
            )
            if row is None:
                raise
            if (row.input_kind, row.filename, row.size_bytes) != (
                input_kind,
                filename,
                size_bytes,
            ) or row.details.get("fingerprint") != (details or {}).get("fingerprint"):
                raise ValueError("This request already belongs to a different input") from None
        else:
            session.add(
                DomainEvent(
                    aggregate_type="content",
                    aggregate_id=str(row.id),
                    event_type="content.received",
                    payload={"channel_profile_id": str(channel_id), "input_kind": input_kind},
                )
            )
        session.refresh(row)
        session.expunge(row)
        return row


def accept_chunk(channel_id: uuid.UUID, item_id: uuid.UUID, offset: int, data: bytes) -> dict:
    digest = hashlib.sha256(data).hexdigest()
    with session_scope() as session:
        row = _item(session, channel_id, item_id)
        if row.input_kind != "file" or row.status != "uploading":
            raise ValueError("This file is no longer accepting upload chunks")
        chunks = list(row.details.get("chunks") or [])
        previous = next((part for part in chunks if part["offset"] == offset), None)
        if previous is not None:
            if previous["sha256"] != digest or previous["size"] != len(data):
                raise ValueError("The selected file differs from the previously uploaded bytes")
            return {"upload_offset": row.upload_offset, "replayed": True}
        remaining = int(row.size_bytes or 0) - row.upload_offset
        if offset != row.upload_offset or not 0 < len(data) <= min(CHUNK_BYTES, remaining):
            raise ValueError("Upload offset or chunk size does not match the saved receipt")
        key = f"intake/{row.id}/{offset}"
        store = ObjectStore()
        store.ensure_bucket()
        store.put_bytes(data, key, "application/octet-stream")
        chunks.append({"offset": offset, "size": len(data), "sha256": digest, "key": key})
        row.details = {**row.details, "chunks": chunks}
        row.upload_offset += len(data)
        row.error = None
        return {"upload_offset": row.upload_offset, "replayed": False}


def finish_upload(channel_id: uuid.UUID, item_id: uuid.UUID) -> dict:
    work = get_settings().work_dir / f"file-intake-{item_id}-{uuid.uuid4().hex}"
    work.mkdir(parents=True)
    try:
        with session_scope() as session:
            row = _item(session, channel_id, item_id)
            if row.clip_id:
                return {"id": str(row.id), "clip_id": str(row.clip_id), "replayed": True}
            if row.input_kind != "file" or row.upload_offset != row.size_bytes:
                raise ValueError("Upload has not finished; reselect the file to resume")
            store = ObjectStore()
            extension = Path(row.filename or "video.mp4").suffix.lower().lstrip(".")
            target = work / f"input.{extension}"
            with target.open("wb") as output:
                for part in row.details.get("chunks") or []:
                    data = store.get_bytes(part["key"])
                    if (
                        len(data) != part["size"]
                        or hashlib.sha256(data).hexdigest() != part["sha256"]
                    ):
                        raise ValueError(
                            "Stored upload chunk failed verification; retry the upload"
                        )
                    output.write(data)
            if target.stat().st_size != row.size_bytes:
                raise ValueError("Uploaded size does not match the receipt")
            probe = ffprobe(target)
            duration, width, height = _extract_video_shape(probe)
            if not width or not height or not duration or duration <= 0:
                raise ValueError(
                    "This file does not contain a playable video; export a video first"
                )
            digest = sha256_file(target)
            clip = session.scalar(select(Clip).where(Clip.sha256 == digest))
            key = clip.storage_key if clip else store.raw_key(digest, extension)
            # Repair missing bytes for a previous deduplicated record, without changing lineage.
            if not store.exists(key):
                store.put_file(target, key)
            if clip is None:
                try:
                    with session.begin_nested():
                        clip = Clip(
                            sha256=digest,
                            storage_key=key,
                            extension=extension,
                            size_bytes=row.size_bytes,
                            duration_seconds=duration,
                            width=width,
                            height=height,
                            status="ingested",
                            media_metadata=probe,
                        )
                        session.add(clip)
                        session.flush()
                except IntegrityError:
                    clip = session.scalar(select(Clip).where(Clip.sha256 == digest))
                    if clip is None:
                        raise
            url = f"katcha-file://{row.id}"
            source = SourceItem(
                source_url=url,
                canonical_url=url,
                platform="local_file",
                status="ready",
                title=row.title,
                clip_id=clip.id,
                source_metadata={
                    "channel_profile_id": str(channel_id),
                    "content_item_id": str(row.id),
                },
            )
            session.add(source)
            session.flush()
            row.source_id, row.clip_id = source.id, clip.id
            row.status, row.error = "ready", None
            row.details = {**row.details, "verified_sha256": digest}
            session.add(
                DomainEvent(
                    aggregate_type="content",
                    aggregate_id=str(row.id),
                    event_type="content.media_ready",
                    payload={"channel_profile_id": str(channel_id), "clip_id": str(clip.id)},
                )
            )
            return {"id": str(row.id), "clip_id": str(clip.id), "replayed": False}
    finally:
        shutil.rmtree(work, ignore_errors=True)


def adopt_channel_sources(channel_id: uuid.UUID) -> None:
    """Give existing acquired/queued source work a stable content receipt on first use."""
    with session_scope() as session:
        sources = list(
            session.scalars(
                select(SourceItem).where(
                    SourceItem.source_metadata["channel_profile_id"].as_string() == str(channel_id)
                )
            )
        )
        for source in sources:
            if session.scalar(
                select(ContentItem.id).where(
                    ContentItem.channel_profile_id == channel_id, ContentItem.source_id == source.id
                )
            ):
                continue
            key = f"source-{source.id}"
            try:
                with session.begin_nested():
                    session.add(
                        ContentItem(
                            channel_profile_id=channel_id,
                            request_key=key,
                            input_kind="source",
                            title=source.title or source.source_url,
                            status="received",
                            source_id=source.id,
                            clip_id=source.clip_id,
                            upload_offset=0,
                            details={},
                        )
                    )
                    session.flush()
            except IntegrityError:
                pass  # Another reader adopted the same source.


def content_detail(session: Session, row: ContentItem) -> dict:
    source = session.get(SourceItem, row.source_id) if row.source_id else None
    clip_id = row.clip_id or (source.clip_id if source else None)
    clip = session.get(Clip, clip_id) if clip_id else None
    productions = (
        list(
            session.scalars(
                select(Production)
                .where(
                    Production.clip_id == clip_id,
                    Production.channel_profile_id == row.channel_profile_id,
                )
                .order_by(Production.created_at.desc())
            )
        )
        if clip_id
        else []
    )
    profile = session.get(ChannelProfile, row.channel_profile_id)
    if profile is None:
        raise ValueError("Channel profile not found")
    publication_rows = (
        list(
            session.scalars(
                select(Publication)
                .where(
                    Publication.production_id.in_([p.id for p in productions]),
                    Publication.youtube_connection_id == profile.youtube_connection_id,
                )
                .order_by(Publication.created_at.desc())
            )
        )
        if productions
        else []
    )
    identities = [("content", str(row.id))]
    identities += [("source", str(source.id))] if source else []
    identities += [("production", str(p.id)) for p in productions]
    identities += [("publication", str(p.id)) for p in publication_rows]
    events = list(
        session.scalars(
            select(DomainEvent)
            .where(
                or_(
                    *[
                        (DomainEvent.aggregate_type == kind)
                        & (DomainEvent.aggregate_id == identity)
                        for kind, identity in identities
                    ]
                )
            )
            .order_by(DomainEvent.created_at.desc())
            .limit(100)
        )
    )
    from katcha.services.acquisition import clip_acquisition_state

    readiness = clip_acquisition_state(clip_id) if clip_id else None
    phase = "media_ready" if clip else (source.status if source else row.status)
    return {
        "id": row.id,
        "channel_profile_id": row.channel_profile_id,
        "input_kind": row.input_kind,
        "request_key": row.request_key,
        "fingerprint": row.details.get("fingerprint"),
        "title": row.title,
        "source_title": source.title if source else None,
        "phase": phase,
        "source_id": row.source_id,
        "clip_id": clip_id,
        "filename": row.filename,
        "source_url": source.source_url if source else row.details.get("url"),
        "size_bytes": row.size_bytes,
        "upload_offset": row.upload_offset,
        "chunk_bytes": CHUNK_BYTES,
        "chunks": [
            {k: part[k] for k in ("offset", "size", "sha256")}
            for part in row.details.get("chunks") or []
        ],
        "error": source.error if source and source.error else row.error,
        "created_at": row.created_at,
        "updated_at": source.updated_at if source else row.updated_at,
        "readiness": {
            "managed": readiness.managed,
            "eligible": readiness.eligible,
            "reason": readiness.reason,
        }
        if readiness
        else None,
        "media": {
            "width": clip.width,
            "height": clip.height,
            "duration_seconds": clip.duration_seconds,
            "size_bytes": clip.size_bytes,
        }
        if clip
        else None,
        "productions": [
            {
                "id": p.id,
                "kind": p.kind,
                "status": p.status,
                "stage": p.stage,
                "generation": p.generation,
                "error": p.error,
            }
            for p in productions
        ],
        "publications": [
            {
                "id": p.id,
                "production_id": p.production_id,
                "title": p.title,
                "status": p.status,
                "stage": p.stage,
                "youtube_video_id": p.youtube_video_id,
                "publish_at": p.publish_at,
                "upload_offset": p.upload_offset,
                "upload_size": p.upload_size,
                "privacy_status": p.privacy_status,
                "error": p.error,
            }
            for p in publication_rows
        ],
        "events": [{"event_type": e.event_type, "created_at": e.created_at} for e in events],
        "href": f"/content?channel={row.channel_profile_id}&item={row.id}",
    }


def list_content(channel_id: uuid.UUID, *, q: str = "", offset: int = 0, limit: int = 25) -> dict:
    adopt_channel_sources(channel_id)
    with session_scope() as session:
        if session.get(ChannelProfile, channel_id) is None:
            raise ValueError("channel profile not found")
        filters = [ContentItem.channel_profile_id == channel_id]
        if q.strip():
            filters.append(ContentItem.title.ilike(f"%{q.strip()}%"))
        total = session.scalar(select(func.count()).select_from(ContentItem).where(*filters)) or 0
        rows = session.scalars(
            select(ContentItem)
            .where(*filters)
            .order_by(ContentItem.created_at.desc(), ContentItem.id)
            .offset(offset)
            .limit(limit)
        )
        return {
            "total": total,
            "offset": offset,
            "limit": limit,
            "items": [content_detail(session, row) for row in rows],
        }


def cleanup_completed_upload(channel_id: uuid.UUID, item_id: uuid.UUID) -> None:
    """Delete staging chunks only after the verified media reference has committed.

    Failed cleanup retains a pending marker and can be retried through /complete.
    Original media and shared clip objects are never deleted here.
    """
    with session_scope() as session:
        row = _item(session, channel_id, item_id)
        if not row.clip_id or row.status != "ready" or row.details.get("staging_cleaned"):
            return
        store = ObjectStore()
        try:
            for part in row.details.get("chunks") or []:
                store.delete(f"intake/{row.id}/{part['offset']}")
        except Exception:
            row.details = {**row.details, "staging_cleanup_pending": True}
            return
        row.details = {**row.details, "staging_cleaned": True, "staging_cleanup_pending": False}


def prepare_unchanged_video(channel_id: uuid.UUID, item_id: uuid.UUID, actor: str) -> Production:
    from katcha.services.productions import register_source_passthrough_production

    # Serialize retries for this receipt, including the interval between production
    # registration and linking it. A crash after the inner commit reuses its workflow ID.
    with session_scope() as session:
        row = _item(session, channel_id, item_id)
        source = session.get(SourceItem, row.source_id) if row.source_id else None
        clip_id = row.clip_id or (source.clip_id if source else None)
        if not clip_id:
            raise ValueError("Wait for the media download or upload to finish")
        production = register_source_passthrough_production(
            clip_id, channel_profile_id=channel_id, idempotency_key=f"manual-{item_id}", actor=actor
        )
        row.production_id = production.id
        return production
