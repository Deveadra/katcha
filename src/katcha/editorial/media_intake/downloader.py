"""Bounded, allowlisted HTTPS fetching for explicitly authorized media sources."""

from __future__ import annotations

import hashlib
import ipaddress
import socket
import tempfile
import uuid
from collections.abc import Callable, Iterator
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import parse_qsl, urljoin, urlsplit

import httpx

from katcha.editorial.media_intake.contracts import ProviderPermissionV1
from katcha.editorial.media_intake.receipts import (
    claim_acquisition,
    record_outcome,
    start_acquisition,
)
from katcha.editorial_models import AcquisitionReceipt
from katcha.integrations.storage import ObjectStore

MAX_MEDIA_BYTES = 200 * 1024 * 1024
MAX_REDIRECTS = 3
SENSITIVE_QUERY_KEYS = {
    "token",
    "access_token",
    "auth",
    "authorization",
    "signature",
    "sig",
    "expires",
    "key",
}


def _validate_fetch_url(url: str, permission: ProviderPermissionV1) -> str:
    try:
        parsed = urlsplit(url)
        host = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        raise ValueError("Media URL is malformed") from exc
    if (
        parsed.scheme != "https"
        or not host
        or parsed.username
        or parsed.password
        or parsed.fragment
        or (port not in {None, 443})
        or not permission.allows_host(host)
    ):
        raise ValueError("Media URL must use HTTPS on an explicitly allowlisted host")
    if any(key.casefold() in SENSITIVE_QUERY_KEYS for key, _ in parse_qsl(parsed.query)):
        raise ValueError("Signed or credential-bearing media URLs are not accepted")
    try:
        direct_ip = ipaddress.ip_address(host)
    except ValueError:
        direct_ip = None
    if direct_ip is not None:
        addresses = [direct_ip]
    else:
        try:
            resolved = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
        except OSError as exc:
            raise ValueError("Media host could not be resolved") from exc
        addresses = [ipaddress.ip_address(item[4][0]) for item in resolved]
    if not addresses or any(not address.is_global for address in addresses):
        raise ValueError("Media host resolves to a non-public address")
    return parsed.geturl()


def _chunks(response: httpx.Response) -> Iterator[bytes]:
    yield from response.iter_bytes()


def fetch_authorized_media(
    channel_id: uuid.UUID,
    candidate_id: uuid.UUID,
    *,
    operation_key: str,
    permission: ProviderPermissionV1,
    fetch_url: str,
    actor: str,
    store: ObjectStore | None = None,
    max_bytes: int = MAX_MEDIA_BYTES,
    client_factory: Callable[..., httpx.Client] = httpx.Client,
) -> AcquisitionReceipt:
    """Fetch once under a durable claim; ambiguous failures stay unknown and are not retried."""
    if max_bytes <= 0 or max_bytes > MAX_MEDIA_BYTES:
        raise ValueError(f"Maximum media size must be between 1 and {MAX_MEDIA_BYTES} bytes")
    if not permission.allows("download"):
        raise ValueError("Provider-specific download permission is absent or expired")
    current_url = _validate_fetch_url(fetch_url, permission)
    receipt = start_acquisition(
        channel_id,
        candidate_id,
        operation_key=operation_key,
        permission=permission,
        fetch_url_if_allowed=current_url,
        actor=actor,
    )
    if receipt.status in {"succeeded", "failed"}:
        return receipt
    claim_acquisition(channel_id, receipt.id)
    object_store = store or ObjectStore()
    received = 0
    request_sent = False
    try:
        with tempfile.NamedTemporaryFile(prefix="katcha-media-", delete=False) as temp:
            temp_path = Path(temp.name)
        try:
            with client_factory(
                timeout=httpx.Timeout(20.0, connect=5.0),
                follow_redirects=False,
                trust_env=False,
            ) as client:
                for redirect_count in range(MAX_REDIRECTS + 1):
                    current_url = _validate_fetch_url(current_url, permission)
                    request_sent = True
                    with client.stream(
                        "GET",
                        current_url,
                        headers={
                            "User-Agent": "KatchaMediaIntake/1.0",
                            "Accept": "video/*, image/*, audio/*",
                        },
                    ) as response:
                        if response.status_code in {301, 302, 303, 307, 308}:
                            if redirect_count >= MAX_REDIRECTS:
                                raise ValueError("Media source exceeded the redirect limit")
                            location = response.headers.get("location")
                            if not location:
                                raise ValueError("Media redirect did not include a destination")
                            current_url = urljoin(current_url, location)
                            continue
                        if response.status_code < 200 or response.status_code >= 300:
                            record_outcome(
                                channel_id,
                                receipt.id,
                                status="failed",
                                response_time=datetime.now(UTC),
                                error_category=f"http_{response.status_code}",
                            )
                            raise ValueError(f"Media provider returned HTTP {response.status_code}")
                        content_length = response.headers.get("content-length")
                        if content_length:
                            try:
                                if int(content_length) > max_bytes:
                                    raise ValueError("Media response exceeds the allowed size")
                            except ValueError as exc:
                                if str(exc) == "Media response exceeds the allowed size":
                                    raise
                                raise ValueError(
                                    "Media response has an invalid content length"
                                ) from exc
                        digest = hashlib.sha256()
                        with temp_path.open("wb") as output:
                            for chunk in _chunks(response):
                                received += len(chunk)
                                if received > max_bytes:
                                    raise ValueError("Media response exceeds the allowed size")
                                digest.update(chunk)
                                output.write(chunk)
                        mime = response.headers.get(
                            "content-type", "application/octet-stream"
                        ).split(";", 1)[0]
                        break
                else:
                    raise ValueError("Media source exceeded the redirect limit")

            sha256 = digest.hexdigest()
            object_key = f"media/sha256/{sha256}"
            if not object_store.exists(object_key):
                object_store.put_file(temp_path, object_key, content_type=mime)
            return record_outcome(
                channel_id,
                receipt.id,
                status="succeeded",
                response_time=datetime.now(UTC),
                bytes_received=received,
                sha256=sha256,
                mime=mime,
                object_store_key=object_key,
            )
        finally:
            temp_path.unlink(missing_ok=True)
    except ValueError:
        # A policy or known HTTP response is a known failure; do not retry under this key.
        with suppress(ValueError):
            record_outcome(
                channel_id,
                receipt.id,
                status="failed",
                response_time=datetime.now(UTC),
                bytes_received=received,
                error_category="policy_or_response_error",
            )
        raise
    except Exception as exc:
        record_outcome(
            channel_id,
            receipt.id,
            status="unknown" if request_sent else "failed",
            response_time=datetime.now(UTC),
            bytes_received=received,
            error_category="transport_or_storage_outcome_unknown",
        )
        raise ValueError(
            "Media acquisition failed; reconcile the durable receipt before retrying"
        ) from exc
