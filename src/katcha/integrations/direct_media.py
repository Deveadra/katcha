"""Bounded, DNS-pinned HTTPS transfer of direct publisher/creator media files.

Discovery metadata is not proof of publication rights or scene identity. This
module only retrieves explicit public HTTPS file links, without cookies, proxies,
redirect credentials, or browser state. Upload and rights gates remain elsewhere.
"""

from __future__ import annotations

import hashlib
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urljoin, urlsplit

from katcha.editorial.retrieval import _PinnedHTTPS, public_addresses

_DIRECT_SUFFIXES = frozenset({".mp4", ".mov", ".m4v", ".webm", ".mkv"})
MAX_DIRECT_BYTES = 4 * 1024**3  # Includes trailer-scale files; requires scratch disk.
READ_CHUNK = 1024 * 1024
MAX_REDIRECTS = 4
MAX_ELAPSED_SECONDS = 7200
SCRATCH_RESERVE_BYTES = 128 * 1024**2


class DirectMediaError(RuntimeError):
    diagnostic_stage = "direct HTTPS media acquisition"
    diagnostic_code = "direct_media_unavailable"
    diagnostic_retryable = False
    diagnostic_recovery = "Try another reachable, authorized direct media source."


class DirectMediaRejected(DirectMediaError):
    diagnostic_code = "direct_media_rejected"


class DirectMediaAccessDenied(DirectMediaError):
    diagnostic_code = "direct_media_access_denied"
    diagnostic_recovery = (
        "The direct media host refused access. Use another accessible authorized source."
    )


class DirectMediaTooLarge(DirectMediaRejected):
    diagnostic_code = "direct_media_exceeds_transfer_limit"
    diagnostic_recovery = (
        "Use the resumable R2/media-bin upload path for larger footage."
    )


class DirectMediaTransportError(DirectMediaError):
    diagnostic_code = "direct_media_transport_failed"
    diagnostic_retryable = True
    diagnostic_recovery = (
        "The direct source failed during transfer; verify the host before retrying."
    )


@dataclass(frozen=True, slots=True)
class DirectMediaResult:
    path: Path
    sha256: str
    size_bytes: int
    extension: str
    content_type: str
    final_host: str
    redirects: int


def is_direct_media_url(url: str) -> bool:
    try:
        parsed = urlsplit(url)
        return Path(parsed.path.casefold()).suffix in _DIRECT_SUFFIXES
    except ValueError:
        return False


def _content_length(value: str | None) -> int | None:
    if value is None:
        return None
    if not value.isascii() or not value.isdecimal():
        raise DirectMediaRejected("The direct source supplied an invalid content length")
    return int(value)


def _validate_media_response(response, declared: int | None) -> str:
    content_type = str(response.getheader("Content-Type") or "").split(";", 1)[0].strip().lower()
    if not (
        content_type.startswith("video/")
        or content_type in {"application/octet-stream", "application/mp4", "binary/octet-stream"}
    ):
        raise DirectMediaRejected("The direct source did not return a video media type")
    if str(response.getheader("Content-Encoding") or "identity").lower() != "identity":
        raise DirectMediaRejected("The direct source returned compressed transfer encoding")
    if declared == 0:
        raise DirectMediaRejected("The direct source returned an empty file")
    if declared is not None and declared > MAX_DIRECT_BYTES:
        raise DirectMediaTooLarge("The direct media file exceeds the 4 GiB limit")
    return content_type


def fetch_direct_media(url: str, job_dir: Path) -> DirectMediaResult:
    """Save at most 4 GiB in 1 MiB chunks, validating each redirect's public IP."""
    if not is_direct_media_url(url):
        raise DirectMediaRejected("Not an explicit direct video-file URL")
    extension = Path(urlsplit(url).path.lower()).suffix
    current_url = url
    started = time.monotonic()

    for redirect_count in range(MAX_REDIRECTS + 1):
        # public_addresses validates HTTPS, credentials, ports and *all* DNS results.
        try:
            host, addresses = public_addresses(current_url)
        except (OSError, ValueError):
            raise DirectMediaRejected(
                "The direct source is not a validated public HTTPS destination"
            ) from None

        parsed = urlsplit(current_url)
        connection = _PinnedHTTPS(host, addresses[0])
        try:
            target = parsed.path or "/"
            if parsed.query:
                target += "?" + parsed.query
            connection.request(
                "GET",
                target,
                headers={
                    "User-Agent": "KatchaMediaAcquisition/1.0",
                    "Accept": "video/*,application/octet-stream",
                    "Accept-Encoding": "identity",
                },
            )
            response = connection.getresponse()
            if response.status in {301, 302, 303, 307, 308}:
                location = response.getheader("Location")
                if not location:
                    raise DirectMediaRejected("The media redirect has no destination")
                # Never transmit cookies or auth headers to either origin.
                current_url = urljoin(current_url, location)
                continue
            if response.status in {401, 403}:
                raise DirectMediaAccessDenied("The direct media host rejected public access")
            if response.status in {400, 404, 410}:
                raise DirectMediaRejected("The direct media file is not available at its URL")
            if response.status != 200:
                raise DirectMediaTransportError(
                    f"The direct media host returned HTTP {response.status}"
                )

            declared = _content_length(response.getheader("Content-Length"))
            content_type = _validate_media_response(response, declared)
            # Fast-fail when the full file cannot fit the known scratch volume.
            if declared is not None and (
                declared + SCRATCH_RESERVE_BYTES > shutil.disk_usage(job_dir).free
            ):
                raise DirectMediaRejected(
                    "Insufficient local acquisition scratch space for the direct file"
                )

            destination = job_dir / ("source" + extension)
            digest = hashlib.sha256()
            total = 0
            with destination.open("xb") as output:
                while True:
                    if time.monotonic() - started > MAX_ELAPSED_SECONDS:
                        raise DirectMediaTransportError(
                            "Direct media transfer exceeded its time budget"
                        )
                    chunk = response.read(READ_CHUNK)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > MAX_DIRECT_BYTES:
                        raise DirectMediaTooLarge("The direct media file exceeds the 4 GiB limit")
                    if declared is not None and total > declared:
                        raise DirectMediaRejected(
                            "The source transferred more bytes than its stated length"
                        )
                    output.write(chunk)
                    digest.update(chunk)

            if not total or (declared is not None and total != declared):
                raise DirectMediaTransportError(
                    "The direct media file was empty or its transfer was incomplete"
                )
            return DirectMediaResult(
                path=destination,
                sha256=digest.hexdigest(),
                size_bytes=total,
                extension=extension.lstrip("."),
                content_type=content_type,
                final_host=host,
                redirects=redirect_count,
            )
        except DirectMediaError:
            raise
        except OSError:
            # Transport errors can include signed URLs or socket details.
            raise DirectMediaTransportError("The direct media HTTPS transfer failed") from None
        finally:
            connection.close()
    raise DirectMediaRejected("The direct media source exceeded the redirect limit")
