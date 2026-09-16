from __future__ import annotations

import hashlib
import uuid
import xml.etree.ElementTree as ET
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import Any, Protocol
from urllib.parse import urlparse

import httpx

from katcha.config import Settings
from katcha.integrations.youtube.client import YouTubeClient

_MAX_FEED_BYTES = 2_000_000


class TrendAdapterError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        kind: str,
        retry_after_seconds: int | None = None,
        transient: bool = True,
    ) -> None:
        super().__init__(message)
        self.kind = kind
        self.retry_after_seconds = retry_after_seconds
        self.transient = transient


@dataclass(frozen=True, slots=True)
class TrendAdapterContext:
    channel_profile_id: uuid.UUID
    youtube_connection_id: uuid.UUID | None
    observed_at: datetime
    settings: Settings


@dataclass(frozen=True, slots=True)
class TrendObservation:
    topic: str
    provider_key: str
    external_id: str
    source_kind: str
    independence_key: str
    canonical_url: str | None = None
    source_name: str | None = None
    title: str | None = None
    body_excerpt: str | None = None
    author: str | None = None
    community: str | None = None
    language: str | None = None
    region: str | None = None
    published_at: datetime | None = None
    metrics: dict[str, float] = field(default_factory=dict)
    media_refs: tuple[dict[str, Any], ...] = ()
    aliases: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()
    content_fingerprint: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    match_confidence: float = 1.0
    match_reasons: tuple[str, ...] = ("adapter_topic_assignment",)


@dataclass(frozen=True, slots=True)
class TrendAdapterBatch:
    observations: tuple[TrendObservation, ...]
    next_cursor: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)


class TrendSourceAdapter(Protocol):
    key: str
    version: str

    def poll(
        self,
        query: dict[str, Any],
        cursor: dict[str, Any],
        context: TrendAdapterContext,
    ) -> TrendAdapterBatch: ...


def _retry_after(response: httpx.Response) -> int | None:
    value = response.headers.get("Retry-After")
    if not value:
        return None
    try:
        return max(0, int(value))
    except ValueError:
        try:
            retry_at = parsedate_to_datetime(value)
        except (TypeError, ValueError, OverflowError):
            return None
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=UTC)
        return max(0, int((retry_at.astimezone(UTC) - datetime.now(UTC)).total_seconds()))


def _require_success(response: httpx.Response, provider: str) -> None:
    if response.status_code == 429:
        raise TrendAdapterError(
            f"{provider} rate limited trend polling",
            kind="rate_limited",
            retry_after_seconds=_retry_after(response),
        )
    if response.status_code >= 500:
        raise TrendAdapterError(
            f"{provider} trend polling returned HTTP {response.status_code}",
            kind="provider_unavailable",
            retry_after_seconds=_retry_after(response),
        )
    if response.is_error:
        raise TrendAdapterError(
            f"{provider} trend polling returned HTTP {response.status_code}",
            kind="provider_rejected_request",
            transient=False,
        )


def _iso_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    cleaned = value.strip()
    if not cleaned:
        return None
    try:
        parsed = parsedate_to_datetime(cleaned)
    except (TypeError, ValueError, OverflowError):
        try:
            parsed = datetime.fromisoformat(cleaned.replace("Z", "+00:00"))
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _fingerprint(*values: object) -> str:
    raw = "\x1f".join(str(value or "") for value in values)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class RedditTrendAdapter:
    key = "reddit"
    version = "v1"

    def __init__(self, transport: httpx.BaseTransport | None = None) -> None:
        self.transport = transport

    def poll(
        self,
        query: dict[str, Any],
        cursor: dict[str, Any],
        context: TrendAdapterContext,
    ) -> TrendAdapterBatch:
        search_query = str(query.get("query") or "").strip()
        if not search_query:
            raise TrendAdapterError(
                "reddit source query requires query",
                kind="invalid_source_query",
                transient=False,
            )
        subreddit = str(query.get("subreddit") or "").strip().strip("/")
        if subreddit.casefold().startswith("r/"):
            subreddit = subreddit[2:]
        topic = str(query.get("topic") or search_query).strip()
        sort = str(query.get("sort") or "new").strip()
        if sort not in {"new", "hot", "top", "relevance", "comments"}:
            raise TrendAdapterError(
                f"unsupported reddit sort: {sort}",
                kind="invalid_source_query",
                transient=False,
            )
        limit = max(1, min(100, int(query.get("limit") or 25)))
        base = (
            f"https://www.reddit.com/r/{subreddit}/search.json"
            if subreddit
            else "https://www.reddit.com/search.json"
        )
        params: dict[str, str | int] = {
            "q": search_query,
            "sort": sort,
            "limit": limit,
            "raw_json": 1,
        }
        if subreddit:
            params["restrict_sr"] = "true"
        if bool(query.get("crawl_pages")) and cursor.get("after"):
            params["after"] = str(cursor["after"])
        headers = {
            "User-Agent": str(
                query.get("user_agent")
                or context.settings.trend_reddit_user_agent
            )
        }
        try:
            with httpx.Client(
                transport=self.transport,
                headers=headers,
                timeout=context.settings.trend_http_timeout_seconds,
                follow_redirects=True,
            ) as client:
                response = client.get(base, params=params)
        except httpx.HTTPError as exc:
            raise TrendAdapterError(
                "reddit trend polling transport failure",
                kind="transport_error",
            ) from exc
        _require_success(response, "reddit")
        try:
            payload = response.json()
        except ValueError as exc:
            raise TrendAdapterError(
                "reddit returned invalid JSON",
                kind="invalid_provider_response",
            ) from exc

        children = payload.get("data", {}).get("children", [])
        observations: list[TrendObservation] = []
        for child in children:
            if not isinstance(child, dict) or not isinstance(child.get("data"), dict):
                continue
            data = child["data"]
            external_id = str(data.get("name") or data.get("id") or "").strip()
            if not external_id:
                continue
            community = str(data.get("subreddit_name_prefixed") or "").strip()
            if not community:
                community = f"r/{data.get('subreddit')}" if data.get("subreddit") else None
            permalink = str(data.get("permalink") or "").strip()
            canonical_url = (
                f"https://www.reddit.com{permalink}" if permalink else None
            )
            published_at = None
            if data.get("created_utc") is not None:
                try:
                    published_at = datetime.fromtimestamp(
                        float(data["created_utc"]), tz=UTC
                    )
                except (TypeError, ValueError, OverflowError):
                    published_at = None
            media_refs: list[dict[str, Any]] = []
            reddit_video = ((data.get("media") or {}).get("reddit_video") or {})
            video_url = str(reddit_video.get("fallback_url") or "").strip()
            if video_url:
                media_refs.append(
                    {
                        "kind": "video",
                        "url": video_url,
                        "source_url": canonical_url,
                        "rights_status": "unassessed",
                    }
                )
            external_media_url = str(data.get("url_overridden_by_dest") or "").strip()
            if external_media_url and data.get("post_hint") in {"image", "hosted:video"}:
                media_refs.append(
                    {
                        "kind": str(data.get("post_hint")),
                        "url": external_media_url,
                        "source_url": canonical_url,
                        "rights_status": "unassessed",
                    }
                )
            title = str(data.get("title") or "").strip() or None
            observations.append(
                TrendObservation(
                    topic=topic,
                    provider_key=self.key,
                    external_id=external_id,
                    source_kind="reddit",
                    independence_key=f"reddit:{community or 'global'}".casefold(),
                    canonical_url=canonical_url,
                    source_name="Reddit",
                    title=title,
                    body_excerpt=(str(data.get("selftext") or "")[:4000] or None),
                    author=(str(data.get("author") or "").strip() or None),
                    community=community,
                    language=(str(query.get("language") or "").strip() or None),
                    region=(str(query.get("region") or "").strip() or None),
                    published_at=published_at,
                    metrics={
                        "score": float(data.get("score") or 0),
                        "upvotes": float(data.get("ups") or 0),
                        "comments": float(data.get("num_comments") or 0),
                    },
                    media_refs=tuple(media_refs),
                    aliases=tuple(str(item) for item in query.get("aliases", []) if item),
                    tags=tuple(
                        dict.fromkeys(
                            [
                                *(
                                    str(item)
                                    for item in query.get("tags", [])
                                    if item
                                ),
                                "reddit",
                                *( [subreddit] if subreddit else [] ),
                            ]
                        )
                    ),
                    content_fingerprint=_fingerprint(title, canonical_url),
                    metadata={
                        "domain": data.get("domain"),
                        "upvote_ratio": data.get("upvote_ratio"),
                        "link_flair_text": data.get("link_flair_text"),
                        "over_18": bool(data.get("over_18")),
                        "spoiler": bool(data.get("spoiler")),
                        "stickied": bool(data.get("stickied")),
                    },
                )
            )
        response_data = payload.get("data") or {}
        next_cursor = {
            "after": response_data.get("after"),
            "last_polled_at": context.observed_at.isoformat(),
        }
        rate_metadata: dict[str, Any] = {}
        for header, key in (
            ("x-ratelimit-remaining", "rate_limit_remaining"),
            ("x-ratelimit-reset", "rate_limit_reset_seconds"),
            ("x-ratelimit-used", "rate_limit_used"),
        ):
            if response.headers.get(header) is not None:
                rate_metadata[key] = response.headers[header]
        return TrendAdapterBatch(
            observations=tuple(observations),
            next_cursor=next_cursor,
            metadata={"provider": self.key, **rate_metadata},
        )


class YouTubeTrendAdapter:
    key = "youtube"
    version = "v1"

    def __init__(
        self,
        client_factory: Callable[[uuid.UUID, Settings], YouTubeClient] | None = None,
    ) -> None:
        self.client_factory = client_factory or (
            lambda connection_id, settings: YouTubeClient(connection_id, settings=settings)
        )

    def poll(
        self,
        query: dict[str, Any],
        cursor: dict[str, Any],
        context: TrendAdapterContext,
    ) -> TrendAdapterBatch:
        del cursor
        search_query = str(query.get("query") or "").strip()
        if not search_query:
            raise TrendAdapterError(
                "youtube source query requires query",
                kind="invalid_source_query",
                transient=False,
            )
        if context.youtube_connection_id is None:
            raise TrendAdapterError(
                "youtube trend polling requires a channel YouTube connection",
                kind="missing_connection",
                transient=False,
            )
        max_results = max(1, min(50, int(query.get("limit") or 25)))
        lookback_hours = max(1, min(720, int(query.get("lookback_hours") or 72)))
        published_after = context.observed_at - timedelta(hours=lookback_hours)
        client = self.client_factory(context.youtube_connection_id, context.settings)
        try:
            search = client.search_videos(
                search_query,
                published_after=published_after,
                max_results=max_results,
                order=str(query.get("order") or "date"),
                region_code=(str(query.get("region") or "").strip() or None),
                relevance_language=(
                    str(query.get("language") or "").strip() or None
                ),
            )
            video_ids = [
                str(item.get("id", {}).get("videoId") or "").strip()
                for item in search.get("items", [])
            ]
            video_ids = [item for item in video_ids if item]
            resources = client.video_resources(video_ids) if video_ids else []
        except Exception as exc:
            status_code = getattr(exc, "status_code", None)
            retry_after = getattr(exc, "retry_after_seconds", None)
            kind = "rate_limited" if status_code == 429 else "provider_error"
            raise TrendAdapterError(
                "youtube trend polling failed",
                kind=kind,
                retry_after_seconds=retry_after,
            ) from exc

        topic = str(query.get("topic") or search_query).strip()
        observations: list[TrendObservation] = []
        for resource in resources:
            video_id = str(resource.get("id") or "").strip()
            if not video_id:
                continue
            snippet = resource.get("snippet") or {}
            statistics = resource.get("statistics") or {}
            channel_id = str(snippet.get("channelId") or "").strip()
            title = str(snippet.get("title") or "").strip() or None
            canonical_url = f"https://www.youtube.com/watch?v={video_id}"
            published_at = _iso_datetime(str(snippet.get("publishedAt") or ""))
            thumbnails = snippet.get("thumbnails") or {}
            thumbnail_refs = [
                {
                    "kind": "thumbnail",
                    "url": value.get("url"),
                    "width": value.get("width"),
                    "height": value.get("height"),
                    "source_url": canonical_url,
                    "rights_status": "unassessed",
                }
                for value in thumbnails.values()
                if isinstance(value, dict) and value.get("url")
            ]
            media_refs = [
                {
                    "kind": "youtube_video",
                    "url": canonical_url,
                    "source_url": canonical_url,
                    "rights_status": "unassessed",
                },
                *thumbnail_refs,
            ]
            observations.append(
                TrendObservation(
                    topic=topic,
                    provider_key=self.key,
                    external_id=video_id,
                    source_kind="youtube",
                    independence_key=f"youtube:{channel_id or video_id}".casefold(),
                    canonical_url=canonical_url,
                    source_name=(str(snippet.get("channelTitle") or "").strip() or None),
                    title=title,
                    body_excerpt=(str(snippet.get("description") or "")[:4000] or None),
                    author=(str(snippet.get("channelTitle") or "").strip() or None),
                    community=(str(snippet.get("channelTitle") or "").strip() or None),
                    language=(str(query.get("language") or "").strip() or None),
                    region=(str(query.get("region") or "").strip() or None),
                    published_at=published_at,
                    metrics={
                        "views": float(statistics.get("viewCount") or 0),
                        "likes": float(statistics.get("likeCount") or 0),
                        "comments": float(statistics.get("commentCount") or 0),
                    },
                    media_refs=tuple(media_refs),
                    aliases=tuple(str(item) for item in query.get("aliases", []) if item),
                    tags=tuple(
                        dict.fromkeys(
                            [
                                *(
                                    str(item)
                                    for item in query.get("tags", [])
                                    if item
                                ),
                                *(str(item) for item in snippet.get("tags", []) if item),
                                "youtube",
                            ]
                        )
                    ),
                    content_fingerprint=_fingerprint(title, channel_id, video_id),
                    metadata={
                        "channel_id": channel_id or None,
                        "category_id": snippet.get("categoryId"),
                        "live_broadcast_content": snippet.get("liveBroadcastContent"),
                        "default_language": snippet.get("defaultLanguage"),
                    },
                )
            )
        return TrendAdapterBatch(
            observations=tuple(observations),
            next_cursor={"last_polled_at": context.observed_at.isoformat()},
            metadata={
                "provider": self.key,
                "quota_units_estimate": 101 if video_ids else 100,
                "result_count": len(observations),
            },
        )


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].casefold()


def _child_text(element: ET.Element, names: set[str]) -> str | None:
    for child in element:
        if _local_name(child.tag) in names and child.text and child.text.strip():
            return child.text.strip()
    return None


def _entry_link(element: ET.Element) -> str | None:
    for child in element:
        if _local_name(child.tag) != "link":
            continue
        href = str(child.attrib.get("href") or "").strip()
        if href:
            rel = str(child.attrib.get("rel") or "alternate").casefold()
            if rel in {"alternate", ""}:
                return href
        if child.text and child.text.strip():
            return child.text.strip()
    return None


class RssTrendAdapter:
    key = "rss"
    version = "v1"

    def __init__(self, transport: httpx.BaseTransport | None = None) -> None:
        self.transport = transport

    def poll(
        self,
        query: dict[str, Any],
        cursor: dict[str, Any],
        context: TrendAdapterContext,
    ) -> TrendAdapterBatch:
        feed_url = str(query.get("feed_url") or "").strip()
        topic = str(query.get("topic") or "").strip()
        if not feed_url or not topic:
            raise TrendAdapterError(
                "rss source query requires feed_url and topic",
                kind="invalid_source_query",
                transient=False,
            )
        parsed = urlparse(feed_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise TrendAdapterError(
                "rss feed_url must be http(s)",
                kind="invalid_source_query",
                transient=False,
            )
        headers: dict[str, str] = {
            "User-Agent": context.settings.trend_feed_user_agent,
        }
        if cursor.get("etag"):
            headers["If-None-Match"] = str(cursor["etag"])
        if cursor.get("last_modified"):
            headers["If-Modified-Since"] = str(cursor["last_modified"])
        try:
            with httpx.Client(
                transport=self.transport,
                headers=headers,
                timeout=context.settings.trend_http_timeout_seconds,
                follow_redirects=True,
            ) as client:
                response = client.get(feed_url)
        except httpx.HTTPError as exc:
            raise TrendAdapterError(
                "rss trend polling transport failure",
                kind="transport_error",
            ) from exc
        if response.status_code == 304:
            return TrendAdapterBatch(
                observations=(),
                next_cursor={
                    **cursor,
                    "last_polled_at": context.observed_at.isoformat(),
                },
                metadata={"provider": self.key, "not_modified": True},
            )
        _require_success(response, "rss")
        if len(response.content) > _MAX_FEED_BYTES:
            raise TrendAdapterError(
                "rss feed exceeds maximum supported size",
                kind="invalid_provider_response",
                transient=False,
            )
        try:
            root = ET.fromstring(response.content)
        except ET.ParseError as exc:
            raise TrendAdapterError(
                "rss provider returned invalid XML",
                kind="invalid_provider_response",
            ) from exc

        source_name = str(query.get("source_name") or parsed.netloc).strip()
        source_kind = str(query.get("source_kind") or "news").strip().casefold()
        independence_key = str(
            query.get("independence_key") or f"rss:{parsed.netloc.casefold()}"
        ).strip()
        entries = [
            element
            for element in root.iter()
            if _local_name(element.tag) in {"item", "entry"}
        ]
        observations: list[TrendObservation] = []
        for entry in entries[: max(1, min(100, int(query.get("limit") or 50)))]:
            title = _child_text(entry, {"title"})
            link = _entry_link(entry)
            external_id = _child_text(entry, {"guid", "id"}) or link or title
            if not external_id:
                continue
            published_raw = _child_text(
                entry,
                {"pubdate", "published", "updated", "date"},
            )
            summary = _child_text(entry, {"description", "summary", "content"})
            author = _child_text(entry, {"author", "creator"})
            categories = [
                (child.text or "").strip()
                for child in entry
                if _local_name(child.tag) == "category" and (child.text or "").strip()
            ]
            media_refs: list[dict[str, Any]] = []
            for child in entry:
                local = _local_name(child.tag)
                if local not in {"enclosure", "thumbnail", "content"}:
                    continue
                media_url = str(
                    child.attrib.get("url") or child.attrib.get("href") or ""
                ).strip()
                if not media_url:
                    continue
                media_refs.append(
                    {
                        "kind": local,
                        "url": media_url,
                        "mime_type": child.attrib.get("type"),
                        "source_url": link or feed_url,
                        "rights_status": "unassessed",
                    }
                )
            observations.append(
                TrendObservation(
                    topic=topic,
                    provider_key=self.key,
                    external_id=str(external_id),
                    source_kind=source_kind,
                    independence_key=independence_key.casefold(),
                    canonical_url=link or feed_url,
                    source_name=source_name,
                    title=title,
                    body_excerpt=summary[:4000] if summary else None,
                    author=author,
                    language=(str(query.get("language") or "").strip() or None),
                    region=(str(query.get("region") or "").strip() or None),
                    published_at=_iso_datetime(published_raw),
                    metrics={"mentions": 1.0},
                    media_refs=tuple(media_refs),
                    aliases=tuple(str(item) for item in query.get("aliases", []) if item),
                    tags=tuple(
                        dict.fromkeys(
                            [
                                *(
                                    str(item)
                                    for item in query.get("tags", [])
                                    if item
                                ),
                                *categories,
                                source_kind,
                            ]
                        )
                    ),
                    content_fingerprint=_fingerprint(title, link, published_raw),
                    metadata={
                        "feed_url": feed_url,
                        "feed_host": parsed.netloc.casefold(),
                    },
                )
            )
        return TrendAdapterBatch(
            observations=tuple(observations),
            next_cursor={
                "etag": response.headers.get("ETag"),
                "last_modified": response.headers.get("Last-Modified"),
                "last_polled_at": context.observed_at.isoformat(),
            },
            metadata={"provider": self.key, "result_count": len(observations)},
        )


_ADAPTERS: dict[tuple[str, str], TrendSourceAdapter] = {}


def register_trend_adapter(adapter: TrendSourceAdapter) -> None:
    key = (adapter.key, adapter.version)
    if key in _ADAPTERS:
        raise ValueError(f"trend adapter already registered: {adapter.key}@{adapter.version}")
    _ADAPTERS[key] = adapter


def get_trend_adapter(key: str, version: str) -> TrendSourceAdapter:
    try:
        return _ADAPTERS[(key, version)]
    except KeyError as exc:
        raise ValueError(f"trend adapter is not installed: {key}@{version}") from exc


def available_trend_adapters() -> list[dict[str, str]]:
    return [
        {"key": key, "version": version}
        for key, version in sorted(_ADAPTERS)
    ]


register_trend_adapter(RedditTrendAdapter())
register_trend_adapter(YouTubeTrendAdapter())
register_trend_adapter(RssTrendAdapter())
