from __future__ import annotations

import ipaddress
import socket
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from urllib.parse import urljoin, urlparse
from xml.etree import ElementTree

import httpx

from katcha.acquisition.adapters import DiscoveredCandidate, DiscoveryBatch

_USER_AGENT = "Katcha/0.1 feed-discovery"
_MAX_REDIRECTS = 3
_ACCEPT_HEADER = ", ".join(
    (
        "application/rss+xml",
        "application/atom+xml",
        "application/xml",
        "text/xml",
    )
)


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].casefold()


def _text(element: ElementTree.Element, *names: str) -> str | None:
    wanted = {name.casefold() for name in names}
    for child in element:
        if _local_name(child.tag) in wanted:
            value = "".join(child.itertext()).strip()
            if value:
                return value
    return None


def _link(element: ElementTree.Element) -> str | None:
    for child in element:
        if _local_name(child.tag) != "link":
            continue
        href = str(child.attrib.get("href") or "").strip()
        rel = str(child.attrib.get("rel") or "alternate").casefold()
        if href and rel in {"alternate", ""}:
            return href
        value = "".join(child.itertext()).strip()
        if value:
            return value
    return None


def _parse_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    raw = value.strip()
    try:
        parsed = parsedate_to_datetime(raw)
    except (TypeError, ValueError, OverflowError):
        parsed = None
    if parsed is None:
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _matches_terms(
    *,
    title: str,
    summary: str,
    include_terms: list[str],
    exclude_terms: list[str],
) -> bool:
    haystack = f"{title} {summary}".casefold()
    includes = [value.strip().casefold() for value in include_terms if value.strip()]
    excludes = [value.strip().casefold() for value in exclude_terms if value.strip()]
    if includes and not any(term in haystack for term in includes):
        return False
    return not any(term in haystack for term in excludes)


def parse_feed(
    content: bytes | str,
    *,
    feed_url: str,
    include_terms: list[str] | None = None,
    exclude_terms: list[str] | None = None,
    limit: int = 100,
) -> tuple[DiscoveredCandidate, ...]:
    if limit < 1:
        raise ValueError("feed discovery limit must be positive")
    try:
        root = ElementTree.fromstring(content)
    except ElementTree.ParseError as exc:
        raise ValueError("feed response is not valid XML") from exc

    root_name = _local_name(root.tag)
    if root_name == "rss":
        channel = next(
            (child for child in root if _local_name(child.tag) == "channel"),
            root,
        )
        entries = [child for child in channel if _local_name(child.tag) == "item"]
        feed_title = _text(channel, "title")
    elif root_name == "feed":
        entries = [child for child in root if _local_name(child.tag) == "entry"]
        feed_title = _text(root, "title")
    else:
        entries = [
            child
            for child in root.iter()
            if _local_name(child.tag) in {"item", "entry"}
        ]
        feed_title = _text(root, "title")

    include = include_terms or []
    exclude = exclude_terms or []
    result: list[DiscoveredCandidate] = []
    for rank, entry in enumerate(entries, start=1):
        title = _text(entry, "title") or ""
        summary = _text(entry, "description", "summary", "content") or ""
        if not _matches_terms(
            title=title,
            summary=summary,
            include_terms=include,
            exclude_terms=exclude,
        ):
            continue
        link = _link(entry)
        if not link:
            continue
        source_url = urljoin(feed_url, link)
        external_id = _text(entry, "guid", "id") or source_url
        creator = _text(entry, "author", "creator")
        published_at = _parse_timestamp(
            _text(entry, "pubdate", "published", "updated", "date")
        )
        metadata: dict[str, object] = {
            "feed_url": feed_url,
            "feed_title": feed_title,
            "feed_rank": rank,
            "summary": summary,
            "source_metrics": {"feed_rank": rank},
        }
        if published_at is not None:
            metadata["published_at"] = published_at.isoformat()
        result.append(
            DiscoveredCandidate(
                source_url=source_url,
                external_id=external_id,
                title=title or None,
                creator=creator,
                provenance_confidence=0.7,
                provenance_claims={
                    "discovered_via": "rss_atom_feed",
                    "feed_url": feed_url,
                },
                metadata=metadata,
            )
        )
        if len(result) >= limit:
            break
    return tuple(result)


def _validate_public_url(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("feed_url must be a public HTTP(S) URL")
    if parsed.username or parsed.password:
        raise ValueError("feed_url must not contain embedded credentials")
    host = parsed.hostname.casefold()
    if host == "localhost" or host.endswith(".local"):
        raise ValueError("feed_url cannot target a local host")
    try:
        direct_ip = ipaddress.ip_address(host)
    except ValueError:
        direct_ip = None
    if direct_ip is not None:
        if not direct_ip.is_global:
            raise ValueError("feed_url cannot target a non-public IP address")
        return

    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        addresses = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise ValueError("feed_url hostname could not be resolved") from exc
    if not addresses:
        raise ValueError("feed_url hostname did not resolve")
    for address in addresses:
        ip = ipaddress.ip_address(address[4][0])
        if not ip.is_global:
            raise ValueError("feed_url resolved to a non-public IP address")


def _fetch_feed(url: str) -> tuple[bytes, str]:
    current = url
    with httpx.Client(timeout=15.0, follow_redirects=False) as client:
        for _ in range(_MAX_REDIRECTS + 1):
            _validate_public_url(current)
            response = client.get(
                current,
                headers={"User-Agent": _USER_AGENT, "Accept": _ACCEPT_HEADER},
            )
            if response.status_code in {301, 302, 303, 307, 308}:
                location = response.headers.get("location")
                if not location:
                    raise ValueError("feed redirect did not include a location")
                current = urljoin(current, location)
                continue
            response.raise_for_status()
            return response.content, str(response.url)
    raise ValueError("feed exceeded maximum redirect count")


class RssAtomDiscoveryAdapter:
    key = "rss_atom"
    version = "v1"

    def discover(
        self,
        query: dict[str, object],
        cursor: dict[str, object],
    ) -> DiscoveryBatch:
        del cursor
        feed_url = str(query.get("feed_url") or "").strip()
        if not feed_url:
            raise ValueError("rss_atom discovery requires feed_url")
        include_terms = [str(value) for value in query.get("include_terms", [])]
        exclude_terms = [str(value) for value in query.get("exclude_terms", [])]
        limit = min(max(int(query.get("limit", 100)), 1), 500)
        content, resolved_url = _fetch_feed(feed_url)
        items = parse_feed(
            content,
            feed_url=resolved_url,
            include_terms=include_terms,
            exclude_terms=exclude_terms,
            limit=limit,
        )
        return DiscoveryBatch(items=items, done=True)
