"""Bounded R2/S3 multipart staging and seekable ranged media inspection.

The direct HTTPS reader sends small chunks directly into multipart upload. This
module never writes source bytes to worker scratch disk. Staged objects are
removed on normal failures; a crashed process still requires bucket lifecycle
cleanup of stale staging keys and incomplete uploads.
"""

from __future__ import annotations

import hashlib
import io
from contextlib import suppress
from decimal import Decimal
from typing import Any

from katcha.integrations.storage import ObjectStore
from katcha.runtime_fence import assert_mutation_authority

PART_BYTES = 8 * 1024 * 1024
READ_AHEAD_BYTES = 4 * 1024 * 1024


class MultipartObjectWriter:
    def __init__(self, store: ObjectStore, key: str, content_type: str = "video/mp4") -> None:
        assert_mutation_authority("storage.multipart_upload", settings=store.settings)
        self.store = store
        self.key = key
        self.buffer = bytearray()
        self.parts: list[dict[str, Any]] = []
        self.size_bytes = 0
        self.finished = False
        upload = store.client.create_multipart_upload(
            Bucket=store.settings.s3_bucket, Key=key, ContentType=content_type
        )
        self.upload_id = str(upload["UploadId"])

    def write(self, chunk: bytes) -> None:
        if self.finished:
            raise RuntimeError("Cannot append to a completed multipart upload")
        if not chunk:
            return
        self.buffer.extend(chunk)
        self.size_bytes += len(chunk)
        while len(self.buffer) >= PART_BYTES:
            self._upload_part(PART_BYTES)

    def _upload_part(self, count: int) -> None:
        assert_mutation_authority("storage.multipart_upload_part", settings=self.store.settings)
        part_number = len(self.parts) + 1
        if part_number > 10000:
            raise ValueError("Multipart part count limit exceeded")
        # One part-size allocation, independent of the media file's total size.
        data = bytes(self.buffer[:count])
        response = self.store.client.upload_part(
            Bucket=self.store.settings.s3_bucket,
            Key=self.key,
            UploadId=self.upload_id,
            PartNumber=part_number,
            Body=data,
        )
        self.parts.append({"PartNumber": part_number, "ETag": response["ETag"]})
        del self.buffer[:count]

    def finish(self) -> int:
        if self.finished or not self.size_bytes:
            raise ValueError("Multipart upload has no new media to complete")
        if self.buffer:
            self._upload_part(len(self.buffer))
        assert_mutation_authority("storage.multipart_upload_complete", settings=self.store.settings)
        self.store.client.complete_multipart_upload(
            Bucket=self.store.settings.s3_bucket,
            Key=self.key,
            UploadId=self.upload_id,
            MultipartUpload={"Parts": self.parts},
        )
        self.finished = True
        stored_size = self.store.stat(self.key)["size_bytes"]
        if stored_size != self.size_bytes:
            raise ValueError("Completed media object length differs from the source stream")
        return stored_size

    def abort(self) -> None:
        if self.finished:
            return
        # Do not hide the original transfer failure behind cleanup problems.
        with suppress(Exception):
            assert_mutation_authority("storage.multipart_upload_abort", settings=self.store.settings)
            self.store.client.abort_multipart_upload(
                Bucket=self.store.settings.s3_bucket,
                Key=self.key,
                UploadId=self.upload_id,
            )


class SeekableObjectReader(io.RawIOBase):
    """4 MiB bounded read-ahead, checked Content-Range, no local file or signed URL."""

    def __init__(self, store: ObjectStore, key: str, size: int) -> None:
        super().__init__()
        self.store = store
        self.key = key
        self.size = size
        self.position = 0
        self.window_start = 0
        self.window = b""

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def tell(self) -> int:
        return self.position

    def seek(self, offset: int, whence: int = io.SEEK_SET) -> int:
        if whence == io.SEEK_SET:
            target = offset
        elif whence == io.SEEK_CUR:
            target = self.position + offset
        elif whence == io.SEEK_END:
            target = self.size + offset
        else:
            raise ValueError("Unsupported media reader seek")
        if target < 0:
            raise ValueError("Negative media reader position")
        self.position = target
        return target

    def read(self, size: int = -1) -> bytes:
        if self.closed:
            raise ValueError("Media reader is closed")
        if self.position >= self.size or size == 0:
            return b""
        count = min(READ_AHEAD_BYTES, self.size - self.position)
        if size > 0:
            count = min(size, count)
        result = bytearray()
        while len(result) < count:
            if not (self.window_start <= self.position < self.window_start + len(self.window)):
                start = self.position
                end = min(self.size - 1, start + READ_AHEAD_BYTES - 1)
                response = self.store.client.get_object(
                    Bucket=self.store.settings.s3_bucket,
                    Key=self.key,
                    Range=f"bytes={start}-{end}",
                )
                expected_range = f"bytes {start}-{end}/{self.size}"
                body = response["Body"]
                try:
                    if response.get("ContentRange") != expected_range:
                        raise ValueError("Object store ignored the requested media range")
                    self.window = body.read(READ_AHEAD_BYTES + 1)
                finally:
                    body.close()
                if len(self.window) != end - start + 1:
                    raise ValueError("Incomplete range read while inspecting the media")
                self.window_start = start
            within = self.position - self.window_start
            payload = self.window[within : within + count - len(result)]
            if not payload:
                raise ValueError("Incomplete seekable media read")
            result.extend(payload)
            self.position += len(payload)
        return bytes(result)


def inspect_stored_media(store: ObjectStore, key: str, size: int) -> dict[str, object]:
    """PyAV's seekable file-object interface handles MP4 metadata at the tail."""
    import av

    with SeekableObjectReader(store, key, size) as reader:
        with av.open(reader, mode="r") as container:
            streams = [
                {
                    "index": int(stream.index),
                    "codec_type": stream.type,
                    "codec_name": str(stream.codec_context.name or ""),
                    "width": int(stream.codec_context.width or 0)
                    if stream.type == "video" else None,
                    "height": int(stream.codec_context.height or 0)
                    if stream.type == "video" else None,
                }
                for stream in container.streams
            ]
            if not any(item["codec_type"] == "video" for item in streams):
                raise ValueError("Stored media has no decodable video stream")
            duration = (
                str(Decimal(container.duration) / Decimal(1_000_000))
                if container.duration is not None else None
            )
            return {
                "format": {
                    "duration": duration,
                    "size": str(size),
                    "format_name": container.format.name if container.format else None,
                },
                "streams": streams,
            }


def verify_stored_sha256(store: ObjectStore, key: str, expected: str, size: int) -> None:
    """Read back R2 bytes to prove persisted content, not just successful part ACKs."""
    actual = hashlib.sha256()
    count = 0
    for chunk in store.iter_bytes(key, chunk_size=1024 * 1024):
        count += len(chunk)
        if count > size:
            raise ValueError("Stored media exceeds the expected length")
        actual.update(chunk)
    if count != size or actual.hexdigest() != expected:
        raise ValueError("Stored media SHA-256 differs from the acquired source")
