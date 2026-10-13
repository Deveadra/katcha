"""Resumable browser-to-object-store video uploads.

Only short-lived signed part URLs reach the browser. SourceItem is the durable,
channel-owned upload journal; canonical clips are registered *after* the object
has been completely received, hashed and probed.
"""
from __future__ import annotations

import hashlib
import json
import math
import uuid
from contextlib import suppress
from pathlib import Path

from botocore.exceptions import ClientError
from sqlalchemy import select

from katcha.config import get_settings
from katcha.db import session_scope
from katcha.domain import ClipStatus, SourceStatus
from katcha.editorial.source_uploads import (
    _ALLOWED_CONTENT_TYPES,
    _ALLOWED_EXTENSIONS,
)
from katcha.integrations.download import ffprobe
from katcha.integrations.storage import ObjectStore
from katcha.models import Clip, DomainEvent, SourceItem
from katcha.runtime_fence import assert_mutation_authority
from katcha.services.channel_profiles import ensure_active_profile
from katcha.services.clip_lifecycle import ensure_lifecycle, refresh_search_document

# 4 GiB avoids the 5 GiB single-copy boundary when promoting staged R2 media.
MAX_UPLOAD_BYTES = 4 * 1024**3
PART_BYTES = 64 * 1024**2
SIGNATURE_TTL_SECONDS = 600


def _client_store() -> ObjectStore:
    settings = get_settings()
    browser_endpoint = settings.s3_browser_endpoint_url
    if not browser_endpoint:
        raise ValueError(
            "Large uploads need KATCHA_S3_BROWSER_ENDPOINT_URL and "
            "a matching bucket CORS policy"
        )
    if settings.env == "production" and not browser_endpoint.startswith("https://"):
        raise ValueError("Production browser storage endpoint must use HTTPS")
    return ObjectStore(settings)


def _source(channel_id: uuid.UUID, source_id: uuid.UUID, *, locked: bool = False):
    statement = select(SourceItem).where(SourceItem.id == source_id)
    if locked:
        statement = statement.with_for_update()
    return statement


def _load(session, channel_id: uuid.UUID, source_id: uuid.UUID, *, locked: bool = False):
    row = session.scalar(_source(channel_id, source_id, locked=locked))
    if (
        row is None
        or row.platform != "upload"
        or (row.source_metadata or {}).get("channel_profile_id") != str(channel_id)
        or not (row.source_metadata or {}).get("multipart")
    ):
        raise ValueError("Upload not found for this channel")
    return row


def _receipt(row: SourceItem) -> dict:
    meta = row.source_metadata or {}
    return {
        "source_id": str(row.id),
        "source_url": row.source_url,
        "clip_id": str(row.clip_id) if row.clip_id else None,
        "filename": meta["original_filename"],
        "title": row.title,
        "size_bytes": meta["size_bytes"],
        "part_bytes": meta["part_bytes"],
        "part_count": meta["part_count"],
        "state": meta["state"],
        "sha256": meta.get("sha256"),
        "duration_seconds": meta.get("duration_seconds"),
        "width": meta.get("width"),
        "height": meta.get("height"),
        "deduplicated": meta.get("deduplicated", False),
    }


def initiate(
    channel_id: uuid.UUID,
    *,
    filename: str,
    title: str | None,
    size_bytes: int,
    content_type: str,
    permitted_use: bool,
    idempotency_key: str,
) -> dict:
    name = Path(filename).name
    extension = Path(name).suffix.lower().lstrip(".")
    if not name or name != filename or extension not in _ALLOWED_EXTENSIONS:
        raise ValueError("Upload an MP4, MOV, M4V, MKV or WebM with a plain filename")
    if content_type not in _ALLOWED_CONTENT_TYPES:
        raise ValueError("Unsupported video content type")
    if not 0 < size_bytes <= MAX_UPLOAD_BYTES:
        raise ValueError("Video must be between 1 byte and 4 GiB")
    if not permitted_use:
        raise ValueError("Confirm you have permission to use this video")
    if not idempotency_key or len(idempotency_key) > 120:
        raise ValueError("A stable upload identity is required")
    store = _client_store()
    source_id = uuid.uuid5(channel_id, f"editorial-source-upload:{idempotency_key}")
    source_url = f"https://upload.katcha.invalid/{source_id}"
    display_title = (title or "").strip() or name
    request_digest = hashlib.sha256(
        json.dumps([name, display_title, size_bytes, content_type, True],
                   separators=(",", ":")).encode()
    ).hexdigest()
    count = math.ceil(size_bytes / PART_BYTES)
    key = f"upload-staging/{channel_id}/{source_id}.{extension}"
    with session_scope() as session:
        ensure_active_profile(session, channel_id)
        row = session.scalar(_source(channel_id, source_id, locked=True))
        if row is not None:
            meta = row.source_metadata or {}
            if (
                meta.get("multipart") is not True
                or meta.get("channel_profile_id") != str(channel_id)
                or meta.get("request_digest") != request_digest
            ):
                raise ValueError("Upload identity was already used for different media")
            return _receipt(row)
        # The S3 initiation and journal insertion are deliberately serialized.
        # R2's abort-incomplete-multipart lifecycle handles the rare crash between
        # remote initiation and DB commit; never silently start a second upload.
        assert_mutation_authority("storage.multipart.initiate", settings=store.settings)
        upload = store.client.create_multipart_upload(
            Bucket=store.settings.s3_bucket,
            Key=key,
            ContentType=content_type,
        )
        upload_id = upload["UploadId"]
        row = SourceItem(
            id=source_id,
            source_url=source_url,
            canonical_url=source_url,
            platform="upload",
            status=SourceStatus.REGISTERED.value,
            title=display_title,
            creator="Operator upload",
            source_metadata={
                "multipart": True,
                "channel_profile_id": str(channel_id),
                "request_digest": request_digest,
                "original_filename": name,
                "content_type": content_type,
                "permitted_use_confirmed": True,
                "size_bytes": size_bytes,
                "part_bytes": PART_BYTES,
                "part_count": count,
                "staging_key": key,
                "upload_id": upload_id,
                "state": "uploading",
            },
        )
        session.add(row)
        session.flush()
        return _receipt(row)


def _parts(store: ObjectStore, meta: dict) -> list[dict]:
    result: list[dict] = []
    marker = 0
    while True:
        kwargs = {
            "Bucket": store.settings.s3_bucket,
            "Key": meta["staging_key"],
            "UploadId": meta["upload_id"],
            "MaxParts": 1000,
        }
        if marker:
            kwargs["PartNumberMarker"] = marker
        response = store.client.list_parts(**kwargs)
        result.extend({
            "part_number": int(part["PartNumber"]),
            "etag": str(part["ETag"]),
            "size": int(part["Size"]),
        } for part in response.get("Parts", []))
        if not response.get("IsTruncated"):
            return result
        new_marker = int(response["NextPartNumberMarker"])
        if new_marker <= marker:
            raise ValueError("Invalid paginated upload inventory")
        marker = new_marker


def status(channel_id: uuid.UUID, source_id: uuid.UUID) -> dict:
    with session_scope() as session:
        row = _load(session, channel_id, source_id)
        receipt = _receipt(row)
        meta = dict(row.source_metadata)
    receipt["parts"] = _parts(_client_store(), meta) if meta["state"] == "uploading" else []
    return receipt


def sign_part(channel_id: uuid.UUID, source_id: uuid.UUID, part_number: int) -> dict:
    store = _client_store()
    with session_scope() as session:
        row = _load(session, channel_id, source_id)
        meta = dict(row.source_metadata)
        if meta["state"] != "uploading":
            raise ValueError("Upload is no longer accepting parts")
        if not 1 <= part_number <= meta["part_count"]:
            raise ValueError("Part number is outside this upload")
    assert_mutation_authority("storage.multipart.sign_part", settings=store.settings)
    url = store._client(store.settings.s3_browser_endpoint_url).generate_presigned_url(
        "upload_part",
        Params={
            "Bucket": store.settings.s3_bucket,
            "Key": meta["staging_key"],
            "UploadId": meta["upload_id"],
            "PartNumber": part_number,
        },
        ExpiresIn=SIGNATURE_TTL_SECONDS,
        HttpMethod="PUT",
    )
    return {"url": url, "part_number": part_number, "expires_seconds": SIGNATURE_TTL_SECONDS}


def _validate_completed_parts(parts: list[dict], meta: dict) -> list[dict]:
    count = int(meta["part_count"])
    if len(parts) != count:
        raise ValueError("Not all video parts have arrived")
    for index, part in enumerate(parts, start=1):
        expected = min(int(meta["part_bytes"]),
                       int(meta["size_bytes"]) - (index - 1) * int(meta["part_bytes"]))
        if part["part_number"] != index or part["size"] != expected:
            raise ValueError("Video parts are missing or have unexpected sizes")
    return [{"PartNumber": part["part_number"], "ETag": part["etag"]} for part in parts]


def _hash_object(store: ObjectStore, key: str, expected_bytes: int) -> str:
    # Stream only; a 3 GiB upload must not consume 3 GiB of OCI disk or RAM.
    digest = hashlib.sha256()
    response = store.client.get_object(Bucket=store.settings.s3_bucket, Key=key)
    if int(response["ContentLength"]) != expected_bytes:
        response["Body"].close()
        raise ValueError("Uploaded video size does not match its declaration")
    total = 0
    body = response["Body"]
    try:
        for chunk in iter(lambda: body.read(8 * 1024**2), b""):
            total += len(chunk)
            if total > expected_bytes:
                raise ValueError("Uploaded object exceeds declared size")
            digest.update(chunk)
    finally:
        body.close()
    if total != expected_bytes:
        raise ValueError("Incomplete uploaded object")
    return digest.hexdigest()


def _probe_object(store: ObjectStore, key: str) -> tuple[dict, float, int, int]:
    # ffprobe seeks against an expiring, server-only GET URL. No large temp file.
    url = store.client.generate_presigned_url(
        "get_object",
        Params={"Bucket": store.settings.s3_bucket, "Key": key},
        ExpiresIn=SIGNATURE_TTL_SECONDS,
    )
    try:
        probe = ffprobe(url)
        streams = probe.get("streams") or []
        video = next(stream for stream in streams if stream.get("codec_type") == "video")
        duration = float((probe.get("format") or {}).get("duration") or 0)
        width, height = int(video.get("width") or 0), int(video.get("height") or 0)
        if duration <= 0 or width <= 0 or height <= 0:
            raise ValueError("Invalid video metadata")
    except Exception:
        # Never return an expiring signed GET URL embedded in subprocess errors.
        raise ValueError("Uploaded object is not decodable video") from None
    return probe, duration, width, height


def finish(channel_id: uuid.UUID, source_id: uuid.UUID, *, actor: str) -> dict:
    store = _client_store()
    with session_scope() as session:
        row = _load(session, channel_id, source_id, locked=True)
        meta = dict(row.source_metadata)
        if meta["state"] == "ready":
            return _receipt(row)
        if meta["state"] not in {"uploading", "verifying"}:
            raise ValueError("Upload is not eligible to complete")
        assert_mutation_authority("storage.multipart.finish", settings=store.settings)
        key = meta["staging_key"]
        if meta["state"] == "uploading":
            # If the completion response was lost, R2 may already have the object.
            if not store.exists(key):
                parts = _parts(store, meta)
                manifest = _validate_completed_parts(parts, meta)
                store.client.complete_multipart_upload(
                    Bucket=store.settings.s3_bucket,
                    Key=key,
                    UploadId=meta["upload_id"],
                    MultipartUpload={"Parts": manifest},
                )
            meta["state"] = "verifying"
            row.source_metadata = meta
            session.flush()
    # Verification outside the DB transaction keeps long streaming reads from
    # holding a PostgreSQL row lock. Another finisher can duplicate reads safely.
    digest = _hash_object(store, key, int(meta["size_bytes"]))
    probe, duration, width, height = _probe_object(store, key)
    extension = Path(meta["original_filename"]).suffix.lower().lstrip(".")
    canonical_key = store.raw_key(digest, extension)
    with session_scope() as session:
        row = _load(session, channel_id, source_id, locked=True)
        meta = dict(row.source_metadata)
        if meta["state"] == "ready":
            return _receipt(row)
        assert_mutation_authority("storage.multipart.promote", settings=store.settings)
        clip = session.scalar(select(Clip).where(Clip.sha256 == digest))
        deduplicated = clip is not None
        if clip is None:
            if not store.exists(canonical_key):
                store.client.copy_object(
                    Bucket=store.settings.s3_bucket,
                    Key=canonical_key,
                    CopySource={"Bucket": store.settings.s3_bucket, "Key": key},
                    MetadataDirective="COPY",
                )
            clip = Clip(
                id=uuid.uuid4(),
                sha256=digest,
                storage_key=canonical_key,
                extension=extension,
                size_bytes=meta["size_bytes"],
                duration_seconds=duration,
                width=width,
                height=height,
                status=ClipStatus.INGESTED.value,
                media_metadata=probe,
            )
            session.add(clip)
            session.flush()
        elif not store.exists(clip.storage_key):
            store.client.copy_object(
                Bucket=store.settings.s3_bucket,
                Key=clip.storage_key,
                CopySource={"Bucket": store.settings.s3_bucket, "Key": key},
                MetadataDirective="COPY",
            )
        lifecycle = ensure_lifecycle(session, clip)
        if lifecycle.lifecycle_state != "hot":
            lifecycle.lifecycle_state = "hot"
            lifecycle.archive_key = None
            lifecycle.archived_at = None
            lifecycle.purged_at = None
        refresh_search_document(session, clip, lifecycle)
        row.clip_id = clip.id
        row.status = SourceStatus.READY.value
        meta.update({
            "state": "ready",
            "sha256": digest,
            "duration_seconds": duration,
            "width": width,
            "height": height,
            "deduplicated": deduplicated,
            "actor": actor,
        })
        row.source_metadata = meta
        session.add(DomainEvent(
            aggregate_type="clip",
            aggregate_id=str(clip.id),
            event_type="clip.operator_uploaded",
            payload={
                "channel_profile_id": str(channel_id),
                "source_id": str(source_id),
                "sha256": digest,
                "size_bytes": meta["size_bytes"],
                "permitted_use_confirmed": True,
                "deduplicated": deduplicated,
                "actor": actor,
            },
        ))
        session.flush()
        result = _receipt(row)
    # Staging can be safely retried by lifecycle cleanup if this delete fails.
    with suppress(ClientError):
        store.delete(key)
    return result


def cancel(channel_id: uuid.UUID, source_id: uuid.UUID) -> dict:
    store = _client_store()
    with session_scope() as session:
        row = _load(session, channel_id, source_id, locked=True)
        meta = dict(row.source_metadata)
        if meta["state"] == "ready":
            raise ValueError("Completed source media cannot be cancelled")
        assert_mutation_authority("storage.multipart.abort", settings=store.settings)
        try:
            store.client.abort_multipart_upload(
                Bucket=store.settings.s3_bucket,
                Key=meta["staging_key"],
                UploadId=meta["upload_id"],
            )
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") != "NoSuchUpload":
                raise
        if store.exists(meta["staging_key"]):
            store.delete(meta["staging_key"])
        meta["state"] = "cancelled"
        row.source_metadata = meta
        session.flush()
        return _receipt(row)
