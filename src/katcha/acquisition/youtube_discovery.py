from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from katcha.acquisition.adapters import DiscoveredCandidate, DiscoveryBatch
from katcha.config import get_settings

_SEARCH_URL = "https://www.googleapis.com/youtube/v3/search"
_VIDEOS_URL = "https://www.googleapis.com/youtube/v3/videos"
_ALLOWED_ORDERS = {"date", "rating", "relevance", "title", "viewCount"}


def _integer(value: object) -> int:
    try:
        return max(int(str(value)), 0)
    except (TypeError, ValueError):
        return 0


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
                    f"https://www.youtube.com/channel/{channel_id}"
                    if channel_id
                    else None
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
    terms = [
        str(value).strip()
        for value in query.get("include_terms", [])
        if str(value).strip()
    ]
    if not terms:
        raise ValueError("youtube discovery requires q or include_terms")
    return " ".join(terms)


class YouTubeDiscoveryAdapter:
    key = "youtube"
    version = "v1"

    def discover(
        self,
        query: dict[str, Any],
        cursor: dict[str, Any],
    ) -> DiscoveryBatch:
        settings = get_settings()
        api_key = settings.youtube_data_api_key
        if not api_key:
            raise ValueError("YouTube discovery is not configured")

        requested_limit = min(max(int(query.get("limit", 25)), 1), 50)
        order = str(query.get("order") or "date").strip()
        if order not in _ALLOWED_ORDERS:
            raise ValueError(f"unsupported YouTube search order: {order}")
        freshness_hours = min(
            max(int(query.get("freshness_horizon_hours", 72)), 1),
            24 * 30,
        )
        published_after = datetime.now(UTC) - timedelta(hours=freshness_hours)
        params: dict[str, object] = {
            "key": api_key,
            "part": "snippet",
            "type": "video",
            "q": _search_query(query),
            "order": order,
            "maxResults": requested_limit,
            "publishedAfter": published_after.isoformat().replace("+00:00", "Z"),
        }
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
            search_response = client.get(_SEARCH_URL, params=params)
            search_response.raise_for_status()
            search_payload = search_response.json()
            ids = [
                str(item.get("id", {}).get("videoId") or "")
                for item in search_payload.get("items", [])
                if isinstance(item, dict)
                and isinstance(item.get("id"), dict)
                and item.get("id", {}).get("videoId")
            ]
            videos_payload: dict[str, Any] = {"items": []}
            if ids:
                detail_response = client.get(
                    _VIDEOS_URL,
                    params={
                        "key": api_key,
                        "part": "snippet,statistics",
                        "id": ",".join(ids),
                    },
                )
                detail_response.raise_for_status()
                videos_payload = detail_response.json()

        candidates = parse_youtube_candidates(search_payload, videos_payload)
        next_page = str(search_payload.get("nextPageToken") or "").strip()
        return DiscoveryBatch(
            items=candidates,
            next_cursor={"page_token": next_page} if next_page else {},
            done=not bool(next_page),
        )
