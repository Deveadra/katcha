"""Bounded public-web still acquisition for Editorial review."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import socket
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

import httpx

from katcha.editorial.images import MAX_IMAGE_BYTES, normalize_image
from katcha.integrations.storage import ObjectStore

MAX_SOURCE_PAGE_BYTES = 2 * 1024 * 1024
MAX_RECEIPT_BYTES = 64 * 1024
MAX_REDIRECTS = 4
_ALLOWED_IMAGE_TYPES = {"image/jpeg", "image/png"}


@dataclass(frozen=True, slots=True)
class ReviewImageReceipt:
    source_url: str
    media_url: str
    storage_key: str
    receipt_key: str
    sha256: str
    width: int
    height: int
    size_bytes: int
    discovery_candidate_id: str

    def as_dict(self) -> dict[str, object]:
        return {
            "medium": "image",
            "source_url": self.source_url,
            "media_url": self.media_url,
            "storage_key": self.storage_key,
            "receipt_key": self.receipt_key,
            "sha256": self.sha256,
            "width": self.width,
            "height": self.height,
            "size_bytes": self.size_bytes,
            "discovery_candidate_id": self.discovery_candidate_id,
            "purpose": "review",
        }


class _ImageMetaParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.urls: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        values = {str(key).casefold(): str(value or "") for key, value in attrs}
        if tag.casefold() == "meta":
            marker = (values.get("property") or values.get("name") or "").casefold()
            if marker in {
                "og:image",
                "og:image:url",
                "twitter:image",
                "twitter:image:src",
            }:
                value = values.get("content", "").strip()
                if value:
                    self.urls.append(value)
        elif tag.casefold() == "link":
            rel = {part.casefold() for part in values.get("rel", "").split()}
            value = values.get("href", "").strip()
            if "image_src" in rel and value:
                self.urls.append(value)


def _assert_public_https(
    url: str,
    *,
    resolver=socket.getaddrinfo,
) -> None:
    try:
        parsed = urlsplit(url)
    except ValueError as exc:
        raise ValueError("Still source URL is invalid") from exc
    if (
        parsed.scheme.casefold() != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.port not in {None, 443}
    ):
        raise ValueError("Automatic still acquisition requires a public HTTPS URL")
    try:
        addresses = {
            item[4][0]
            for item in resolver(parsed.hostname, 443, type=socket.SOCK_STREAM)
        }
    except OSError as exc:
        raise ValueError("Still source hostname could not be resolved") from exc
    if not addresses:
        raise ValueError("Still source hostname did not resolve")
    for value in addresses:
        try:
            address = ipaddress.ip_address(value)
        except ValueError as exc:
            raise ValueError("Still source resolved to an invalid address") from exc
        if not address.is_global:
            raise ValueError("Automatic still acquisition blocks private or reserved networks")


def _bounded_get(
    client: httpx.Client,
    url: str,
    *,
    max_bytes: int,
    resolver=socket.getaddrinfo,
) -> tuple[str, str, bytes]:
    current = url
    for _ in range(MAX_REDIRECTS + 1):
        _assert_public_https(current, resolver=resolver)
        with client.stream(
            "GET",
            current,
            headers={
                "Accept": "image/png,image/jpeg,text/html;q=0.8",
                "User-Agent": "KatchaEditorial/1.0",
            },
            follow_redirects=False,
            timeout=20.0,
        ) as response:
            if response.status_code in {301, 302, 303, 307, 308}:
                location = response.headers.get("location")
                if not location:
                    raise ValueError("Still source redirect omitted its destination")
                current = urljoin(current, location)
                continue
            response.raise_for_status()
            length = response.headers.get("content-length")
            if length:
                try:
                    if int(length) > max_bytes:
                        raise ValueError("Remote still resource exceeds the allowed size")
                except ValueError as exc:
                    if "exceeds" in str(exc):
                        raise
            body = bytearray()
            for chunk in response.iter_bytes():
                if len(body) + len(chunk) > max_bytes:
                    raise ValueError("Remote still resource exceeds the allowed size")
                body.extend(chunk)
            content_type = response.headers.get("content-type", "").split(";", 1)[0].casefold()
            return str(response.url), content_type, bytes(body)
    raise ValueError("Still source redirected too many times")


def _declared_image_url(page_url: str, html: bytes) -> str:
    try:
        text = html.decode("utf-8", errors="replace")
    except Exception as exc:
        raise ValueError("Still source page could not be decoded") from exc
    parser = _ImageMetaParser()
    parser.feed(text)
    for raw in parser.urls:
        value = urljoin(page_url, raw)
        if value:
            return value
    raise ValueError(
        "The grounded source page does not declare an OpenGraph/Twitter still for acquisition"
    )


def _receipt_from_store(
    store: ObjectStore,
    receipt_key: str,
    *,
    expected_source_url: str,
    expected_candidate_id: str,
) -> ReviewImageReceipt | None:
    if not store.exists(receipt_key):
        return None
    try:
        payload = json.loads(
            store.get_bytes(receipt_key, max_bytes=MAX_RECEIPT_BYTES).decode("utf-8")
        )
        receipt = ReviewImageReceipt(
            source_url=str(payload["source_url"]),
            media_url=str(payload["media_url"]),
            storage_key=str(payload["storage_key"]),
            receipt_key=receipt_key,
            sha256=str(payload["sha256"]),
            width=int(payload["width"]),
            height=int(payload["height"]),
            size_bytes=int(payload["size_bytes"]),
            discovery_candidate_id=str(payload["discovery_candidate_id"]),
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ValueError("Saved still-acquisition receipt is invalid") from exc
    if (
        receipt.source_url != expected_source_url
        or receipt.discovery_candidate_id != expected_candidate_id
    ):
        raise ValueError("Saved still-acquisition receipt does not match this candidate")
    if not store.exists(receipt.storage_key):
        raise ValueError("Saved still-acquisition receipt is missing its image object")
    return receipt


def acquire_review_image(
    *,
    project_id: str,
    candidate_key: str,
    source_url: str,
    discovery_candidate_id: str,
    store: ObjectStore | None = None,
    client: httpx.Client | None = None,
    resolver=socket.getaddrinfo,
) -> ReviewImageReceipt:
    """Acquire one grounded still for review without granting reuse permission."""

    object_store = store or ObjectStore()
    root = f"editorial/{project_id}/asset-review/{candidate_key}"
    receipt_key = f"{root}/receipt.json"
    previous = _receipt_from_store(
        object_store,
        receipt_key,
        expected_source_url=source_url,
        expected_candidate_id=discovery_candidate_id,
    )
    if previous is not None:
        return previous

    owned_client = client is None
    http = client or httpx.Client()
    try:
        final_url, content_type, body = _bounded_get(
            http,
            source_url,
            max_bytes=max(MAX_SOURCE_PAGE_BYTES, MAX_IMAGE_BYTES),
            resolver=resolver,
        )
        if content_type in _ALLOWED_IMAGE_TYPES:
            media_url = final_url
            encoded, width, height = normalize_image(body)
        elif content_type in {"text/html", "application/xhtml+xml"}:
            if len(body) > MAX_SOURCE_PAGE_BYTES:
                raise ValueError("Still source page exceeds the allowed size")
            media_url = _declared_image_url(final_url, body)
            resolved_url, image_type, image_body = _bounded_get(
                http,
                media_url,
                max_bytes=MAX_IMAGE_BYTES,
                resolver=resolver,
            )
            if image_type not in _ALLOWED_IMAGE_TYPES:
                raise ValueError("Declared still is not a PNG or JPEG")
            media_url = resolved_url
            encoded, width, height = normalize_image(image_body)
        else:
            raise ValueError("Still source must be a PNG/JPEG or an HTML page declaring one")
    finally:
        if owned_client:
            http.close()

    digest = hashlib.sha256(encoded).hexdigest()
    storage_key = f"{root}/{digest}.png"
    if not object_store.exists(storage_key):
        object_store.put_bytes(encoded, storage_key, content_type="image/png")
    receipt = ReviewImageReceipt(
        source_url=source_url,
        media_url=media_url,
        storage_key=storage_key,
        receipt_key=receipt_key,
        sha256=digest,
        width=width,
        height=height,
        size_bytes=len(encoded),
        discovery_candidate_id=discovery_candidate_id,
    )
    object_store.put_bytes(
        json.dumps(receipt.as_dict(), sort_keys=True, separators=(",", ":")).encode(),
        receipt_key,
        content_type="application/json",
    )
    return receipt
