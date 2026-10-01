from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import unquote, urlparse

import httpx
from sqlalchemy import select

from katcha.acquisition.adapters import (
    DiscoveredCandidate,
    DiscoveryAdapterCapability,
    DiscoveryBatch,
)
from katcha.acquisition.http_errors import (
    provider_payload_error,
    provider_response_error,
    provider_transport_error,
)
from katcha.config import get_settings
from katcha.db import session_scope
from katcha.domain import YouTubeConnectionStatus
from katcha.integrations.youtube.tokens import (
    YouTubeCredentialError,
    get_valid_access_token,
)
from katcha.publishing_models import YouTubeConnection

_SEARCH_URL = "https://www.googleapis.com/youtube/v3/search"
_VIDEOS_URL = "https://www.googleapis.com/youtube/v3/videos"
_CHANNELS_URL = "https://www.googleapis.com/youtube/v3/channels"
_ALLOWED_ORDERS = {"date", "rating", "relevance", "title", "viewCount"}


def _integer(value: object) -> int:
    try:
        return max(int(str(value)), 0)
    except (TypeError, ValueError):
        return 0


def _provider_get_json(
    client: httpx.Client,
    url: str,
    *,
    params: dict[str, object],
    operation: str,
    headers: dict[str, str] | None = None,
    provider_usage: dict[str, int] | None = None,
) -> dict[str, Any]:
    try:
        response = client.get(url, params=params, headers=headers)
    except httpx.HTTPError as exc:
        raise provider_transport_error("YouTube", operation, provider_usage=provider_usage) from exc
    if response.status_code >= 400:
        raise provider_response_error(
            "YouTube",
            operation,
            response,
            provider_usage=provider_usage,
        )
    try:
        payload = response.json()
    except ValueError as exc:
        raise provider_payload_error("YouTube", operation, provider_usage=provider_usage) from exc
    if not isinstance(payload, dict):
        raise provider_payload_error("YouTube", operation, provider_usage=provider_usage)
    return payload


def _youtube_request_auth(
    query: dict[str, Any],
) -> tuple[dict[str, object], dict[str, str]]:
    """Resolve a secret-safe credential for public YouTube Data API reads."""
    settings = get_settings()
    requested_connection = str(query.get("youtube_connection_id") or "").strip()
    api_key = str(settings.youtube_data_api_key or "").strip()

    connection_id: uuid.UUID | None = None
    if requested_connection:
        try:
            connection_id = uuid.UUID(requested_connection)
        except ValueError as exc:
            raise ValueError("youtube_connection_id is not a valid UUID") from exc
    elif api_key:
        return {"key": api_key}, {}
    else:
        with session_scope() as session:
            connection_id = session.scalar(
                select(YouTubeConnection.id)
                .where(
                    YouTubeConnection.status
                    == YouTubeConnectionStatus.ACTIVE.value
                )
                .order_by(
                    YouTubeConnection.updated_at.desc(),
                    YouTubeConnection.id.asc(),
                )
                .limit(1)
            )

    if connection_id is not None:
        try:
            access_token = get_valid_access_token(
                connection_id,
                settings=settings,
            )
        except YouTubeCredentialError:
            access_token = ""
        if access_token:
            return {}, {"Authorization": f"Bearer {access_token}"}

    if api_key:
        return {"key": api_key}, {}

    raise ValueError(
        "YouTube discovery is not configured. Connect a YouTube channel "
        "or add a YouTube Data API key, then try again."
    )

def parse_youtube_candidates(
    search_payload: dict[str, Any],
    videos_payload: dict[str, Any],
) -> tuple[DiscoveredCandidate, ...]:
    details_by_id: dict[str, dict[str, Any]] = {}
    for raw in videos_payload.get("items", []):
        if not isinstance(raw, dict):
            continue
        video_id = str(raw.get("id") or "").strip()
        if video_id:
            details_by_id[video_id] = raw

    candidates: list[DiscoveredCandidate] = []
    for rank, raw in enumerate(search_payload.get("items", []), start=1):
        if not isinstance(raw, dict):
            continue
        raw_id = raw.get("id")
        if not isinstance(raw_id, dict):
            continue
        video_id = str(raw_id.get("videoId") or "").strip()
        if not video_id:
            continue
        search_snippet = raw.get("snippet")
        if not isinstance(search_snippet, dict):
            search_snippet = {}
        detail = details_by_id.get(video_id, {})
        snippet = detail.get("snippet")
        if not isinstance(snippet, dict):
            snippet = search_snippet
        statistics = detail.get("statistics")
        if not isinstance(statistics, dict):
            statistics = {}

        title = str(snippet.get("title") or "").strip() or None
        channel_title = str(snippet.get("channelTitle") or "").strip() or None
        channel_id = str(snippet.get("channelId") or "").strip() or None
        published_at = str(snippet.get("publishedAt") or "").strip() or None
        description = str(snippet.get("description") or "").strip()
        metrics = {
            "views": _integer(statistics.get("viewCount")),
            "likes": _integer(statistics.get("likeCount")),
            "comments": _integer(statistics.get("commentCount")),
            "search_rank": rank,
        }
        metadata: dict[str, Any] = {
            "youtube_video_id": video_id,
            "channel_id": channel_id,
            "description": description,
            "search_rank": rank,
            "source_metrics": metrics,
        }
        if published_at:
            metadata["published_at"] = published_at

        candidates.append(
            DiscoveredCandidate(
                source_url=f"https://www.youtube.com/watch?v={video_id}",
                external_id=video_id,
                title=title,
                creator=channel_title,
                creator_url=(
                    f"https://www.youtube.com/channel/{channel_id}" if channel_id else None
                ),
                provenance_confidence=0.95,
                provenance_claims={
                    "discovered_via": "youtube_data_api",
                    "youtube_video_id": video_id,
                    "channel_id": channel_id,
                },
                metadata=metadata,
            )
        )
    return tuple(candidates)


def _search_query(query: dict[str, Any]) -> str:
    explicit = str(query.get("q") or "").strip()
    if explicit:
        return explicit
    terms = [str(value).strip() for value in query.get("include_terms", []) if str(value).strip()]
    if not terms:
        raise ValueError("youtube discovery requires q or include_terms")
    return " ".join(terms)


def youtube_channel_selector(reference: str) -> dict[str, str]:
    value = reference.strip()
    if "://" in value:
        parsed = urlparse(value)
        if (
            parsed.scheme != "https"
            or parsed.hostname
            not in {
                "youtube.com",
                "www.youtube.com",
                "m.youtube.com",
            }
            or parsed.username
            or parsed.password
        ):
            raise ValueError("Use a YouTube channel link, @handle, or channel ID")
        parts = unquote(parsed.path).strip("/").split("/")
        if parts[0].startswith("@"):
            value = parts[0]
        elif len(parts) >= 2 and parts[0] == "channel":
            value = parts[1]
        else:
            raise ValueError("Use the channel's @handle or /channel/ link, not a video link")
    if re.fullmatch(r"UC[A-Za-z0-9_-]{22}", value):
        return {"id": value}
    if (
        value.startswith("@")
        and 1 <= len(value[1:]) <= 100
        and not any(char.isspace() or char in "/?#" for char in value[1:])
    ):
        return {"forHandle": value}
    raise ValueError("Use a YouTube @handle, channel link, or UC channel ID")


class YouTubeDiscoveryAdapter:
    key = "youtube"
    version = "v1"
    capability = DiscoveryAdapterCapability(
        key=key,
        version=version,
        label="YouTube Search",
        description=(
            "Searches YouTube Data API for fresh videos and enriches results "
            "with channel identity and engagement metrics."
        ),
        source_types=("public_api", "video_search"),
        supported_platforms=("youtube",),
        query_fields=(
            "q",
            "channel_reference",
            "include_terms",
            "order",
            "freshness_horizon_hours",
            "relevance_language",
            "region_code",
            "youtube_connection_id",
            "limit",
        ),
        required_credentials=("YOUTUBE_DATA_API_KEY",),
        sample_query={
            "q": "new game trailer",
            "order": "date",
            "freshness_horizon_hours": 72,
            "region_code": "US",
            "limit": 25,
        },
    )

    def discover(
        self,
        query: dict[str, Any],
        cursor: dict[str, Any],
    ) -> DiscoveryBatch:
        auth_params, auth_headers = _youtube_request_auth(query)

        requested_limit = min(max(int(query.get("limit", 25)), 1), 50)
        order = str(query.get("order") or "date").strip()
        if order not in _ALLOWED_ORDERS:
            raise ValueError(f"unsupported YouTube search order: {order}")
        requested_freshness = int(query.get("freshness_horizon_hours", 72))
        freshness_hours = (
            min(max(requested_freshness, 1), 24 * 30)
            if requested_freshness > 0
            else 0
        )
        params: dict[str, object] = {
            **auth_params,
            "part": "snippet",
            "type": "video",
            "order": order,
            "maxResults": requested_limit,
        }
        if freshness_hours:
            published_after = datetime.now(UTC) - timedelta(hours=freshness_hours)
            params["publishedAfter"] = published_after.isoformat().replace(
                "+00:00",
                "Z",
            )
        channel_reference = str(query.get("channel_reference") or "").strip()
        if not channel_reference:
            params["q"] = _search_query(query)
        elif str(query.get("q") or "").strip():
            params["q"] = str(query["q"]).strip()
        language = str(query.get("relevance_language") or "").strip()
        region = str(query.get("region_code") or "").strip().upper()
        page_token = str(cursor.get("page_token") or "").strip()
        if language:
            params["relevanceLanguage"] = language
        if region:
            params["regionCode"] = region
        if page_token:
            params["pageToken"] = page_token

        with httpx.Client(timeout=15.0) as client:
            channel_lookup_units = 0
            channel_id = ""
            if channel_reference:
                selector = youtube_channel_selector(channel_reference)
                channel_id = str(cursor.get("channel_id") or selector.get("id") or "")
                if not channel_id:
                    channel_payload = _provider_get_json(
                        client,
                        _CHANNELS_URL,
                        params={**auth_params, "part": "id", **selector},
                        operation="channel lookup",
                        headers=auth_headers,
                        provider_usage={"youtube.core": 1},
                    )
                    channel_lookup_units = 1
                    channel_id = str(
                        next(
                            (
                                row.get("id")
                                for row in channel_payload.get("items", [])
                                if isinstance(row, dict) and row.get("id")
                            ),
                            "",
                        )
                    )
                    if not channel_id:
                        raise ValueError(
                            "YouTube channel was not found. Check its @handle or link."
                        )
                params["channelId"] = channel_id
            search_payload = _provider_get_json(
                client,
                _SEARCH_URL,
                params=params,
                operation="search",
                headers=auth_headers,
                provider_usage={"youtube.search.list": 1, "youtube.core": channel_lookup_units},
            )
            ids = [
                str(item.get("id", {}).get("videoId") or "")
                for item in search_payload.get("items", [])
                if isinstance(item, dict)
                and isinstance(item.get("id"), dict)
                and item.get("id", {}).get("videoId")
            ]
            videos_payload: dict[str, Any] = {"items": []}
            if ids:
                videos_payload = _provider_get_json(
                    client,
                    _VIDEOS_URL,
                    params={
                        **auth_params,
                        "part": "snippet,statistics",
                        "id": ",".join(ids),
                    },
                    operation="video details",
                    headers=auth_headers,
                    provider_usage={
                        "youtube.search.list": 1,
                        "youtube.core": 1 + channel_lookup_units,
                    },
                )

        candidates = parse_youtube_candidates(search_payload, videos_payload)
        next_page = str(search_payload.get("nextPageToken") or "").strip()
        usage = {"youtube.search.list": 1}
        if ids or channel_lookup_units:
            usage["youtube.core"] = int(bool(ids)) + channel_lookup_units
        return DiscoveryBatch(
            items=candidates,
            next_cursor={"page_token": next_page, "channel_id": channel_id} if next_page else {},
            done=not bool(next_page),
            provider_usage=usage,
        )
