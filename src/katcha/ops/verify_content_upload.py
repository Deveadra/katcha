"""Fail-closed read-only proof of a completed Content upload's canonical R2 video.

This command never creates, moves or deletes media, mutates the database, or grants
publication rights. A metadata receipt alone is insufficient for verification.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import tempfile
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

from sqlalchemy import select

from katcha.clip_lifecycle_models import ClipLifecycle
from katcha.content_models import ContentItem
from katcha.db import session_scope
from katcha.integrations.download import ffprobe
from katcha.integrations.storage import ObjectStore
from katcha.models import Clip, SourceItem

MAX_VERIFY_BYTES = 200 * 1024 * 1024
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_PRODUCTION_BUCKET = "katcha-media-prod"


class VerificationBlocked(ValueError):
    """Operator-safe reason code, without storage keys, URLs or credentials."""


@dataclass(frozen=True)
class ContentEvidence:
    channel_id: uuid.UUID
    content_id: uuid.UUID | None
    source_id: uuid.UUID
    clip_id: uuid.UUID
    object_key: str
    size_bytes: int
    sha256: str
    duration_seconds: float
    width: int
    height: int
    evidence_digest: str
    upload_kind: str = "content"


def _snapshot(channel_id: uuid.UUID, content_id: uuid.UUID) -> ContentEvidence:
    """Read all authorization and identity evidence, with no database writes."""
    with session_scope() as session:
        row = session.scalar(
            select(ContentItem).where(
                ContentItem.id == content_id,
                ContentItem.channel_profile_id == channel_id,
            )
        )
        if row is None:
            raise VerificationBlocked("content_not_found_in_channel")
        if (
            row.input_kind != "file"
            or row.status != "ready"
            or row.source_id is None
            or row.clip_id is None
            or not isinstance(row.size_bytes, int)
            or not 0 < row.size_bytes <= MAX_VERIFY_BYTES
            or row.upload_offset != row.size_bytes
        ):
            raise VerificationBlocked("content_not_completed_or_outside_verify_budget")
        source = session.get(SourceItem, row.source_id)
        clip = session.get(Clip, row.clip_id)
        if source is None or clip is None:
            raise VerificationBlocked("linked_source_or_clip_missing")
        metadata = dict(source.source_metadata or {})
        receipt = dict(row.details or {})
        if (
            source.platform != "local_file"
            or source.status != "ready"
            or source.clip_id != clip.id
            or source.source_url != f"katcha-file://{row.id}"
            or source.canonical_url != source.source_url
            or metadata.get("channel_profile_id") != str(channel_id)
            or metadata.get("content_item_id") != str(row.id)
            or receipt.get("rights_confirmed") is not True
            or not str(receipt.get("rights_confirmed_by") or "").strip()
        ):
            raise VerificationBlocked("content_source_or_permission_evidence_invalid")
        if (
            not isinstance(clip.sha256, str)
            or not _SHA256.fullmatch(clip.sha256)
            or receipt.get("verified_sha256") != clip.sha256
            or clip.size_bytes != row.size_bytes
            or clip.status == "failed"
            or not clip.storage_key
        ):
            raise VerificationBlocked("clip_identity_or_verified_digest_invalid")
        lifecycle = session.get(ClipLifecycle, clip.id)
        if lifecycle is not None and lifecycle.lifecycle_state != "hot":
            raise VerificationBlocked("clip_not_hot")
        try:
            duration = float(clip.duration_seconds or 0)
            width, height = int(clip.width or 0), int(clip.height or 0)
        except (TypeError, ValueError) as exc:
            raise VerificationBlocked("clip_video_metadata_invalid") from exc
        if duration <= 0 or width <= 0 or height <= 0:
            raise VerificationBlocked("clip_video_metadata_invalid")
        # The full receipt digest detects changed permissions, source linkage and
        # canonical Clip identity if an operator edits them while bytes stream.
        seal = hashlib.sha256(
            json.dumps(
                {
                    "receipt": receipt,
                    "source_metadata": metadata,
                    "source_status": source.status,
                    "clip_status": clip.status,
                    "clip_id": str(clip.id),
                    "source_id": str(source.id),
                    "object_key": clip.storage_key,
                    "sha256": clip.sha256,
                    "size_bytes": clip.size_bytes,
                    "duration": str(clip.duration_seconds),
                    "width": width,
                    "height": height,
                    "content_status": row.status,
                    "upload_offset": row.upload_offset,
                    "lifecycle": lifecycle.lifecycle_state if lifecycle else "hot",
                },
                sort_keys=True,
                default=str,
            ).encode()
        ).hexdigest()
        return ContentEvidence(
            channel_id=channel_id,
            content_id=row.id,
            source_id=source.id,
            clip_id=clip.id,
            object_key=clip.storage_key,
            size_bytes=row.size_bytes,
            sha256=clip.sha256,
            duration_seconds=duration,
            width=width,
            height=height,
            evidence_digest=seal,
        )



def _editorial_snapshot(channel_id: uuid.UUID, source_id: uuid.UUID) -> ContentEvidence:
    """Check the original Editorial source-upload or completed multipart journal."""
    with session_scope() as session:
        source = session.get(SourceItem, source_id)
        if (
            source is None
            or source.platform != "upload"
            or source.status != "ready"
            or source.clip_id is None
            or source.source_url != f"https://upload.katcha.invalid/{source.id}"
            or source.canonical_url != source.source_url
        ):
            raise VerificationBlocked("editorial_source_not_completed")
        metadata = dict(source.source_metadata or {})
        if (
            metadata.get("channel_profile_id") != str(channel_id)
            or metadata.get("permitted_use_confirmed") is not True
            or not str(metadata.get("actor") or "").strip()
        ):
            raise VerificationBlocked("editorial_source_permission_missing")
        clip = session.get(Clip, source.clip_id)
        if (
            clip is None
            or not clip.storage_key
            or clip.status == "failed"
            or not isinstance(clip.sha256, str)
            or not _SHA256.fullmatch(clip.sha256)
            or not isinstance(clip.size_bytes, int)
            or not 0 < clip.size_bytes <= MAX_VERIFY_BYTES
        ):
            raise VerificationBlocked("editorial_source_clip_invalid_or_oversized")
        if metadata.get("multipart") is True:
            if (
                metadata.get("state") != "ready"
                or metadata.get("sha256") != clip.sha256
                or metadata.get("size_bytes") != clip.size_bytes
                or not isinstance(metadata.get("part_count"), int)
                or metadata["part_count"] <= 0
            ):
                raise VerificationBlocked("editorial_multipart_completion_invalid")
        elif not (
            isinstance(metadata.get("upload_request_sha256"), str)
            and _SHA256.fullmatch(metadata["upload_request_sha256"])
        ):
            raise VerificationBlocked("editorial_source_upload_identity_missing")
        lifecycle = session.get(ClipLifecycle, clip.id)
        if lifecycle is not None and lifecycle.lifecycle_state != "hot":
            raise VerificationBlocked("clip_not_hot")
        try:
            duration = float(clip.duration_seconds or 0)
            width, height = int(clip.width or 0), int(clip.height or 0)
        except (TypeError, ValueError) as exc:
            raise VerificationBlocked("clip_video_metadata_invalid") from exc
        if duration <= 0 or width <= 0 or height <= 0:
            raise VerificationBlocked("clip_video_metadata_invalid")
        seal = hashlib.sha256(
            json.dumps(
                {
                    "source_id": str(source.id),
                    "clip_id": str(clip.id),
                    "source_metadata": metadata,
                    "source_status": source.status,
                    "clip_status": clip.status,
                    "object_key": clip.storage_key,
                    "sha256": clip.sha256,
                    "size_bytes": clip.size_bytes,
                    "duration": str(clip.duration_seconds),
                    "width": width,
                    "height": height,
                    "lifecycle": lifecycle.lifecycle_state if lifecycle else "hot",
                },
                sort_keys=True,
                default=str,
            ).encode()
        ).hexdigest()
        return ContentEvidence(
            channel_id=channel_id,
            content_id=None,
            source_id=source.id,
            clip_id=clip.id,
            object_key=clip.storage_key,
            size_bytes=clip.size_bytes,
            sha256=clip.sha256,
            duration_seconds=duration,
            width=width,
            height=height,
            evidence_digest=seal,
            upload_kind="editorial",
        )


def _require_production_store(store: ObjectStore) -> None:
    settings = store.settings
    parsed = urlsplit(str(settings.s3_endpoint_url or ""))
    host = parsed.hostname or ""
    if (
        settings.s3_bucket != _PRODUCTION_BUCKET
        or parsed.scheme != "https"
        or not host.endswith(".r2.cloudflarestorage.com")
    ):
        raise VerificationBlocked("not_configured_for_production_r2")


def _pyav_probe(path: Path) -> dict[str, object]:
    """Decode one actual video frame when the external ffprobe binary is unavailable."""
    import av

    with av.open(str(path), mode="r") as container:
        video = next((stream for stream in container.streams if stream.type == "video"), None)
        if video is None:
            raise ValueError("no video stream")
        frame = next(container.decode(video=video.index), None)
        if frame is None:
            raise ValueError("no decodable video frame")
        duration = (
            float(container.duration / av.time_base)
            if container.duration is not None
            else (
                float(video.duration * video.time_base)
                if video.duration is not None and video.time_base is not None
                else 0.0
            )
        )
        return {
            "format": {"duration": duration},
            "streams": [
                {
                    "codec_type": "video",
                    "width": frame.width,
                    "height": frame.height,
                }
            ],
        }


def _probe(path: Path, expected: ContentEvidence) -> dict[str, object]:
    try:
        try:
            report = ffprobe(path)
        except FileNotFoundError:
            # The CI Python runner and some minimal installations have PyAV but
            # lack the ffprobe executable. Only that specific absence triggers
            # the bounded decode fallback; corrupt media still fails closed.
            report = _pyav_probe(path)
        videos = [
            stream
            for stream in (report.get("streams") or [])
            if isinstance(stream, dict) and stream.get("codec_type") == "video"
        ]
        duration = float((report.get("format") or {}).get("duration") or 0)
        video = videos[0] if videos else {}
        width = int(video.get("width") or 0)
        height = int(video.get("height") or 0)
    except Exception as exc:
        raise VerificationBlocked("video_probe_unavailable_or_invalid") from exc
    if (
        not videos
        or duration <= 0
        or width != expected.width
        or height != expected.height
        or abs(duration - expected.duration_seconds) > 0.5
    ):
        raise VerificationBlocked("video_probe_does_not_match_clip")
    return {
        "duration_seconds": duration,
        "width": width,
        "height": height,
        "video_stream_count": len(videos),
    }


def verify_content_upload(
    channel_id: uuid.UUID,
    content_id: uuid.UUID,
    *,
    expected_sha256: str | None = None,
    store: ObjectStore | None = None,
) -> dict[str, object]:
    """Verify one Content receipt plus actual R2 bytes; never grant render rights."""
    return _verify_media(
        lambda: _snapshot(channel_id, content_id), expected_sha256=expected_sha256, store=store
    )


def verify_editorial_upload(
    channel_id: uuid.UUID,
    source_id: uuid.UUID,
    *,
    expected_sha256: str | None = None,
    store: ObjectStore | None = None,
) -> dict[str, object]:
    """Verify an Editorial source-upload receipt, not an arbitrary provider URL."""
    return _verify_media(
        lambda: _editorial_snapshot(channel_id, source_id),
        expected_sha256=expected_sha256,
        store=store,
    )


def _verify_media(
    current_snapshot: Callable[[], ContentEvidence],
    *,
    expected_sha256: str | None,
    store: ObjectStore | None,
) -> dict[str, object]:
    if expected_sha256 is not None and not _SHA256.fullmatch(expected_sha256):
        raise VerificationBlocked("expected_sha256_invalid")
    before = current_snapshot()
    if expected_sha256 is not None and expected_sha256 != before.sha256:
        raise VerificationBlocked("expected_sha256_does_not_match_receipt")
    storage = store if store is not None else ObjectStore()
    _require_production_store(storage)
    # No HeadBucket or ListBucket requirement. R2 object reads alone are needed.
    try:
        remote = storage.stat(before.object_key)
    except Exception as exc:
        raise VerificationBlocked("r2_object_stat_unavailable") from exc
    if remote.get("size_bytes") != before.size_bytes:
        raise VerificationBlocked("r2_object_size_mismatch")
    with tempfile.TemporaryDirectory(prefix="katcha-upload-evidence-") as staging:
        path = Path(staging) / "verified-video"
        digest = hashlib.sha256()
        transferred = 0
        try:
            with path.open("wb") as output:
                for chunk in storage.iter_bytes(before.object_key, chunk_size=1024 * 1024):
                    if not isinstance(chunk, bytes) or not chunk:
                        raise VerificationBlocked("r2_stream_invalid")
                    transferred += len(chunk)
                    if transferred > min(before.size_bytes, MAX_VERIFY_BYTES):
                        raise VerificationBlocked("r2_stream_over_budget")
                    digest.update(chunk)
                    output.write(chunk)
        except VerificationBlocked:
            raise
        except Exception as exc:
            raise VerificationBlocked("r2_stream_unavailable") from exc
        if transferred != before.size_bytes or digest.hexdigest() != before.sha256:
            raise VerificationBlocked("r2_bytes_or_digest_mismatch")
        video = _probe(path, before)
    after = current_snapshot()
    if after != before:
        raise VerificationBlocked("content_evidence_changed_during_verification")
    return {
        "format_version": 1,
        "status": "verified",
        "verification_level": "live_r2_bytes_and_ffprobe_read_only",
        "verified_at": datetime.now(UTC).isoformat(),
        "channel_id": str(before.channel_id),
        "content_id": str(before.content_id) if before.content_id else None,
        "upload_kind": before.upload_kind,
        "source_id": str(before.source_id),
        "clip_id": str(before.clip_id),
        "sha256": before.sha256,
        "size_bytes": transferred,
        "video": video,
        "editorial_index_size_eligible": True,
        "original_sha256_compared": expected_sha256 is not None,
        "permission_recorded": True,
        "rights_independently_verified": False,
        "production_cleared": False,
        "real_editorial_index_executed": False,
        "real_remotion_render_verified": False,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--channel-id", type=uuid.UUID, required=True)
    identity = parser.add_mutually_exclusive_group(required=True)
    identity.add_argument("--content-id", type=uuid.UUID)
    identity.add_argument("--source-id", type=uuid.UUID)
    parser.add_argument("--expected-sha256", default=None)
    args = parser.parse_args(argv)
    try:
        if args.content_id is not None:
            result = verify_content_upload(
                args.channel_id, args.content_id, expected_sha256=args.expected_sha256
            )
        else:
            result = verify_editorial_upload(
                args.channel_id, args.source_id, expected_sha256=args.expected_sha256
            )
    except VerificationBlocked as exc:
        print(json.dumps({"format_version": 1, "status": "blocked", "code": str(exc)}))
        return 2
    except Exception:
        # Never echo a connection string, R2 endpoint, signed URL or provider
        # exception. Unknown outcomes remain blocked, never guessed successful.
        print(json.dumps({"format_version": 1, "status": "blocked", "code": "check_unavailable"}))
        return 3
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
