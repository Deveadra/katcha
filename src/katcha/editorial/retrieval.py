"""Bounded HTTPS retrieval pinned to validated public IPs; no proxy/redirect bypass."""

from __future__ import annotations

import hashlib
import http.client
import ipaddress
import re
import socket
from datetime import UTC, datetime
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

MAX_BYTES = 2_000_000


class _Text(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.hidden = 0
        self.parts: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style", "noscript"}:
            self.hidden += 1

    def handle_endtag(self, tag):
        if tag in {"script", "style", "noscript"}:
            self.hidden = max(0, self.hidden - 1)

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def public_addresses(url: str) -> tuple[str, list[str]]:
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.port not in {None, 443}
    ):
        raise ValueError("Research sources must be public HTTPS pages without credentials")
    host = parsed.hostname.encode("idna").decode("ascii")
    addresses = list(
        dict.fromkeys(item[4][0] for item in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM))
    )
    if not addresses or any(
        (not ipaddress.ip_address(ip).is_global or ipaddress.ip_address(ip).is_multicast)
        for ip in addresses
    ):
        raise ValueError("Research source resolved to a non-public address")
    return host, addresses


class _PinnedHTTPS(http.client.HTTPSConnection):
    def __init__(self, host: str, address: str):
        super().__init__(host, timeout=15)
        self.address = address

    def connect(self):
        # Connect to the validated literal IP, retaining hostname certificate/SNI validation.
        sock = socket.create_connection((self.address, 443), timeout=self.timeout)
        try:
            self.sock = self._context.wrap_socket(sock, server_hostname=self.host)
        except BaseException:
            sock.close()
            raise


def retrieve_document(url: str) -> dict:
    original = url
    for _ in range(4):
        host, addresses = public_addresses(url)
        parsed = urlsplit(url)
        connection = _PinnedHTTPS(host, addresses[0])
        try:
            path = parsed.path or "/"
            if parsed.query:
                path += "?" + parsed.query
            connection.request(
                "GET",
                path,
                headers={
                    "User-Agent": "KatchaEditorialResearch/1.0",
                    "Accept-Encoding": "identity",
                    "Accept": "text/html,text/plain",
                },
            )
            response = connection.getresponse()
            if response.status in {301, 302, 303, 307, 308}:
                location = response.getheader("Location")
                if not location:
                    raise ValueError("Research redirect has no destination")
                url = urljoin(url, location)
                continue
            if response.status != 200:
                raise ValueError(f"Research source is unavailable (HTTP {response.status})")
            content_type = response.getheader("Content-Type", "").lower()
            if not any(content_type.startswith(kind) for kind in ("text/html", "text/plain")):
                raise ValueError("Research source is not a readable HTML/text page")
            if response.getheader("Content-Encoding", "identity").lower() != "identity":
                raise ValueError("Research source sent unsupported compressed content")
            raw = response.read(MAX_BYTES + 1)
            if len(raw) > MAX_BYTES:
                raise ValueError("Research source exceeds the retrieval size limit")
        finally:
            connection.close()
        decoded = raw.decode("utf-8", errors="replace")
        if content_type.startswith("text/html"):
            parser = _Text()
            parser.feed(decoded)
            decoded = " ".join(parser.parts)
        text = re.sub(r"\s+", " ", decoded).strip()
        if len(text) < 80:
            raise ValueError("Research source has insufficient readable evidence")
        return {
            "url": url,
            "original_url": original,
            "text": text[:24000],
            "truncated": len(text) > 24000,
            "sha256": hashlib.sha256(raw).hexdigest(),
            "retrieved_at": datetime.now(UTC).isoformat(),
        }
    raise ValueError("Research source exceeded the redirect limit")
