"""Direct operator media intake into the shared Clip Library."""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select

from katcha.db import session_scope
from katcha.domain import ClipStatus, SourceStatus
from katcha.integrations.download import ffprobe
from katcha.integrations.storage import ObjectStore
from katcha.models import Clip, DomainEvent, SourceItem
from katcha.services.channel_profiles import ensure_active_profile
from katcha.services.clip_lifecycle import ensure_lifecycle, refresh_search_document

MAX_SOURCE_UPLOAD_BYTES = 512 * 1024 * 1024
_ALLOWED_EXTENSIONS = {"mp4", "mov", "m4v", "mkv", "webm"}
_ALLOWED_CONTENT_TYPES = {
    "application/octet-stream",
    "video/mp4",
    "video/quicktime",
    "video/webm",
    "video/x-matroska",
}


@dataclass(frozen=True, slots=True)
class ImportedSourceMedia:
    source_id: uuid.UUID
    clip_id: uuid.UUID
    source_url: str
    title: str
    filename: str
    sha256: str
    size_bytes: int
    duration_seconds: float
    width: int
    height: int
    extension: str
    deduplicated: bool


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _probe_video(path: Path) -> tuple[dict[str, object], float, int, int]:
    try:
        probe = ffprobe(path)
    except Exception as exc:
        raise ValueError("Uploaded source could not be decoded as video") from exc
    streams = list(probe.get("streams") or [])
    video = next((row for row in streams if row.get("codec_type") == "video"), None)
    if video is None:
        raise ValueError("Uploaded source must contain a video stream")
    try:
        duration = float((probe.get("format") or {}).get("duration") or 0)
        width = int(video.get("width") or 0)
        height = int(video.get("height") or 0)
    except (TypeError, ValueError) as exc:
        raise ValueError("Uploaded source has invalid media metadata") from exc
    if duration <= 0 or width <= 0 or height <= 0:
        raise ValueError("Uploaded source must have measurable duration and dimensions")
    return probe, duration, width, height


def import_source_media(
    channel_id: uuid.UUID,
    path: Path,
    *,
    filename: str,
    content_type: str,
    title: str | None,
    permitted_use: bool,
    idempotency_key: str,
    actor: str,
) -> ImportedSourceMedia:
    clean_name = Path(filename).name
    if clean_name != filename or not clean_name:
        raise ValueError("Upload filename must be a plain file name")
    extension = Path(clean_name).suffix.lower().lstrip(".")
    if extension not in _ALLOWED_EXTENSIONS:
        raise ValueError("Upload MP4, MOV, M4V, MKV, or WebM video")
    if content_type not in _ALLOWED_CONTENT_TYPES:
        raise ValueError("Unsupported video content type")
    if not permitted_use:
        raise ValueError("Confirm you have permission to use this source media")
    size_bytes = path.stat().st_size
    if size_bytes <= 0:
        raise ValueError("Uploaded source is empty")
    if size_bytes > MAX_SOURCE_UPLOAD_BYTES:
        raise ValueError("Upload a source video no larger than 512 MiB")

    probe, duration, width, height = _probe_video(path)
    digest = _sha256(path)
    key = ObjectStore.raw_key(digest, extension)
    source_id = uuid.uuid5(channel_id, f"editorial-source-upload:{idempotency_key}")
    source_url = f"https://upload.katcha.invalid/{source_id}"
    display_title = (title or "").strip() or clean_name
    request_digest = hashlib.sha256(
        json.dumps(
            {
                "sha256": digest,
                "filename": clean_name,
                "content_type": content_type,
                "title": display_title,
                "permitted_use": True,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()

    store = ObjectStore()
    replay_source_exists = False
    with session_scope() as session:
        ensure_active_profile(session, channel_id)
        previous = session.get(SourceItem, source_id)
        if previous is not None:
            metadata = dict(previous.source_metadata or {})
            if metadata.get("upload_request_sha256") != request_digest:
                raise ValueError("Upload identity was already used for different source media")
            clip = session.get(Clip, previous.clip_id) if previous.clip_id else None
            if clip is None:
                raise ValueError("Previous source upload is missing its managed clip")
            lifecycle = ensure_lifecycle(session, clip)
            if lifecycle.lifecycle_state == "hot" and store.exists(clip.storage_key):
                return ImportedSourceMedia(
                    source_id=previous.id,
                    clip_id=clip.id,
                    source_url=previous.source_url,
                    title=previous.title or display_title,
                    filename=str(metadata.get("original_filename") or clean_name),
                    sha256=clip.sha256,
                    size_bytes=int(clip.size_bytes or size_bytes),
                    duration_seconds=float(clip.duration_seconds or duration),
                    width=int(clip.width or width),
                    height=int(clip.height or height),
                    extension=clip.extension or extension,
                    deduplicated=True,
                )
            replay_source_exists = True

    if not store.exists(key):
        store.put_file(path, key, content_type=content_type)

    with session_scope() as session:
        ensure_active_profile(session, channel_id)
        clip = session.scalar(select(Clip).where(Clip.sha256 == digest))
        deduplicated = clip is not None
        if clip is None:
            clip = Clip(
                id=uuid.uuid4(),
                sha256=digest,
                storage_key=key,
                extension=extension,
                size_bytes=size_bytes,
                duration_seconds=duration,
                width=width,
                height=height,
                status=ClipStatus.INGESTED.value,
                media_metadata=probe,
            )
            session.add(clip)
            session.flush()
        source = session.get(SourceItem, source_id)
        if source is None:
            source = SourceItem(
                id=source_id,
                source_url=source_url,
                canonical_url=source_url,
                platform="upload",
                status=SourceStatus.READY.value,
                title=display_title,
                creator="Operator upload",
                source_metadata={
                    "channel_profile_id": str(channel_id),
                    "original_filename": clean_name,
                    "content_type": content_type,
                    "permitted_use_confirmed": True,
                    "upload_request_sha256": request_digest,
                    "actor": actor,
                },
                clip_id=clip.id,
            )
            session.add(source)
        lifecycle = ensure_lifecycle(session, clip)
        stale_archive_key = lifecycle.archive_key
        if lifecycle.lifecycle_state != "hot":
            lifecycle.lifecycle_state = "hot"
            lifecycle.archive_key = None
            lifecycle.archived_at = None
            lifecycle.purged_at = None
        refresh_search_document(session, clip, lifecycle)
        session.add(
            DomainEvent(
                aggregate_type="clip",
                aggregate_id=str(clip.id),
                event_type=(
                    "clip.operator_reuploaded"
                    if replay_source_exists
                    else "clip.operator_uploaded"
                ),
                payload={
                    "channel_profile_id": str(channel_id),
                    "source_id": str(source_id),
                    "source_url": source_url,
                    "filename": clean_name,
                    "sha256": digest,
                    "size_bytes": size_bytes,
                    "permitted_use_confirmed": True,
                    "deduplicated": deduplicated,
                    "actor": actor,
                },
            )
        )
        session.flush()
        clip_id = clip.id

    if stale_archive_key and stale_archive_key != key and store.exists(stale_archive_key):
        store.delete(stale_archive_key)

    return ImportedSourceMedia(
        source_id=source_id,
        clip_id=clip_id,
        source_url=source_url,
        title=display_title,
        filename=clean_name,
        sha256=digest,
        size_bytes=size_bytes,
        duration_seconds=duration,
        width=width,
        height=height,
        extension=extension,
        deduplicated=deduplicated,
    )
