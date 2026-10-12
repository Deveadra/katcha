"""Content-based media integrity checks and channel-scoped duplicate evidence."""

from __future__ import annotations

import hashlib
import json
import subprocess
import tempfile
import uuid
import warnings
from pathlib import Path
from typing import Any

from PIL import Image, UnidentifiedImageError
from sqlalchemy import select

from katcha.db import session_scope
from katcha.editorial_models import AcquisitionReceipt, MediaAssetValidation
from katcha.integrations.download import ffprobe
from katcha.integrations.storage import ObjectStore

MAX_VALIDATION_BYTES = 200 * 1024 * 1024
MAX_DURATION_SECONDS = 24 * 60 * 60
MAX_DIMENSION = 16384
MAX_IMAGE_PIXELS = 100_000_000
PROBE_VERSION = 1


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _image_hash(image: Image.Image) -> str:
    pixels = list(image.convert("L").resize((9, 8)).getdata())
    bits = 0
    for row in range(8):
        for column in range(8):
            bits = (bits << 1) | int(pixels[row * 9 + column] > pixels[row * 9 + column + 1])
    return f"{bits:016x}"


def _hamming_distance(left: str, right: str) -> int:
    return (int(left, 16) ^ int(right, 16)).bit_count()


def _image_probe(path: Path) -> tuple[dict[str, Any], str, int, int, str]:
    with warnings.catch_warnings():
        warnings.simplefilter("error", Image.DecompressionBombWarning)
        with Image.open(path) as image:
            width, height = image.size
            if width <= 0 or height <= 0 or width > MAX_DIMENSION or height > MAX_DIMENSION:
                raise ValueError("Image dimensions are outside the supported range")
            if width * height > MAX_IMAGE_PIXELS:
                raise ValueError("Image pixel count exceeds the supported limit")
            image.verify()
    with Image.open(path) as image:
        width, height = image.size
        image_format = (image.format or "").upper()
        if image_format not in {"JPEG", "PNG", "WEBP", "GIF", "TIFF", "BMP"}:
            raise ValueError("Image format is not supported")
        mime = Image.MIME.get(image_format, "application/octet-stream")
        return (
            {"format": image_format, "width": width, "height": height},
            mime,
            width,
            height,
            _image_hash(image),
        )


def _av_probe(
    path: Path, expected_type: str
) -> tuple[dict[str, Any], str, float, int | None, int | None]:
    details = ffprobe(path)
    raw_streams = details.get("streams")
    streams = (
        [item for item in raw_streams if isinstance(item, dict)]
        if isinstance(raw_streams, list)
        else []
    )
    stream_type = "video" if expected_type == "video" else "audio"
    stream = next((item for item in streams if item.get("codec_type") == stream_type), None)
    if stream is None:
        raise ValueError(f"File does not contain a {stream_type} stream")
    raw_format = details.get("format")
    media_format = raw_format if isinstance(raw_format, dict) else {}
    duration = float(media_format.get("duration") or stream.get("duration") or 0)
    if not 0 < duration <= MAX_DURATION_SECONDS:
        raise ValueError("Media duration is outside the supported range")
    if expected_type == "video":
        video_width = int(stream.get("width") or 0)
        video_height = int(stream.get("height") or 0)
        if (
            not 0 < video_width <= MAX_DIMENSION
            or not 0 < video_height <= MAX_DIMENSION
        ):
            raise ValueError("Video dimensions are outside the supported range")
        width: int | None = video_width
        height: int | None = video_height
    else:
        width = None
        height = None
    format_name = str(media_format.get("format_name") or "").casefold()
    mime = (
        "video/mp4"
        if expected_type == "video" and any(name in format_name for name in ("mp4", "mov"))
        else "video/webm"
        if expected_type == "video" and "webm" in format_name
        else "video/x-matroska"
        if expected_type == "video" and "matroska" in format_name
        else "audio/mpeg"
        if expected_type == "audio" and "mp3" in format_name
        else "audio/wav"
        if expected_type == "audio" and "wav" in format_name
        else "audio/ogg"
        if expected_type == "audio" and "ogg" in format_name
        else "application/octet-stream"
    )
    return details, mime, duration, width, height


def _compatible_mime(declared: str | None, actual: str) -> bool:
    if not declared or declared == "application/octet-stream":
        return True
    return declared.split("/", 1)[0] == actual.split("/", 1)[0]


def validate_acquired_media(
    channel_id: uuid.UUID,
    receipt_id: uuid.UUID,
    *,
    expected_type: str,
    store: ObjectStore | None = None,
) -> MediaAssetValidation:
    """Probe actual stored bytes, record exact/perceptual duplicates within the channel."""
    if expected_type not in {"video", "image", "audio"}:
        raise ValueError("Choose video, image, or audio for integrity validation")
    object_store = store or ObjectStore()
    with session_scope() as session:
        receipt = session.scalar(
            select(AcquisitionReceipt).where(
                AcquisitionReceipt.id == receipt_id,
                AcquisitionReceipt.channel_profile_id == channel_id,
                AcquisitionReceipt.status == "succeeded",
            )
        )
        if receipt is None or not receipt.object_store_key or not receipt.sha256:
            raise ValueError("A successful channel-owned acquisition receipt is required")
        existing_rows = session.scalars(
            select(MediaAssetValidation)
            .where(
                MediaAssetValidation.receipt_id == receipt_id,
                MediaAssetValidation.channel_profile_id == channel_id,
            )
            .order_by(MediaAssetValidation.probe_version.desc())
        ).all()
        try:
            stat = object_store.stat(receipt.object_store_key)
            stored_size = int(stat["size_bytes"])
        except (AttributeError, KeyError, TypeError):
            stored_size = -1
        if stored_size <= 0 or stored_size > MAX_VALIDATION_BYTES:
            raise ValueError("Stored media size is missing or outside the supported limit")
        with tempfile.TemporaryDirectory(prefix="katcha-media-probe-") as directory:
            path = Path(directory) / "source.bin"
            object_store.download_file(receipt.object_store_key, path)
            size_bytes = path.stat().st_size
            if size_bytes <= 0 or size_bytes > MAX_VALIDATION_BYTES:
                raise ValueError("Stored media size is outside the supported limit")
            actual_sha = _sha256_file(path)
            latest = existing_rows[0] if existing_rows else None
            if (
                latest is not None
                and latest.actual_sha256 == actual_sha
                and latest.media_type == expected_type
            ):
                return latest

            probe: dict[str, Any] = {}
            width = height = None
            duration = None
            perceptual_hash = None
            error_category = None
            actual_mime = "application/octet-stream"
            try:
                if expected_type == "image":
                    probe, actual_mime, width, height, perceptual_hash = _image_probe(path)
                else:
                    probe, actual_mime, duration, width, height = _av_probe(path, expected_type)
                if not _compatible_mime(receipt.mime, actual_mime):
                    raise ValueError("Declared media type does not match the probed content")
            except (
                ValueError,
                UnidentifiedImageError,
                Image.DecompressionBombError,
                Image.DecompressionBombWarning,
                OSError,
                subprocess.SubprocessError,
                json.JSONDecodeError,
            ):
                error_category = "content_probe_failed"

            status = (
                "validated"
                if error_category is None and actual_sha == receipt.sha256
                else "rejected"
            )
            if actual_sha != receipt.sha256:
                error_category = "content_hash_mismatch"
            duplicate_of_id = None
            if status == "validated":
                exact = session.scalar(
                    select(MediaAssetValidation)
                    .where(
                        MediaAssetValidation.channel_profile_id == channel_id,
                        MediaAssetValidation.actual_sha256 == actual_sha,
                        MediaAssetValidation.status == "validated",
                    )
                    .order_by(MediaAssetValidation.created_at.asc())
                )
                if exact is not None:
                    duplicate_of_id = exact.id
                elif perceptual_hash is not None:
                    prior = session.scalars(
                        select(MediaAssetValidation).where(
                            MediaAssetValidation.channel_profile_id == channel_id,
                            MediaAssetValidation.media_type == "image",
                            MediaAssetValidation.status == "validated",
                            MediaAssetValidation.perceptual_hash.is_not(None),
                        )
                    ).all()
                    for item in prior:
                        same_shape = (
                            item.width
                            and item.height
                            and width
                            and height
                            and abs((item.width / item.height) - (width / height)) <= 0.01
                        )
                        distance = _hamming_distance(
                            item.perceptual_hash or "0", perceptual_hash
                        )
                        if same_shape and distance <= 4:
                            duplicate_of_id = item.id
                            break
            row = MediaAssetValidation(
                id=uuid.uuid4(),
                receipt_id=receipt_id,
                channel_profile_id=channel_id,
                probe_version=(latest.probe_version + 1) if latest else PROBE_VERSION,
                status=status,
                media_type=expected_type,
                expected_sha256=receipt.sha256,
                actual_sha256=actual_sha,
                perceptual_hash=perceptual_hash,
                duplicate_of_id=duplicate_of_id,
                mime=actual_mime,
                size_bytes=size_bytes,
                duration_seconds=duration,
                width=width,
                height=height,
                probe=probe,
                error_category=error_category,
            )
            session.add(row)
            session.flush()
            return row
