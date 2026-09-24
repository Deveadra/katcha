from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from katcha.acquisition.adapters import DiscoveredCandidate, DiscoveryBatch
from katcha.acquisition.http_errors import (
    provider_payload_error,
    provider_response_error,
    provider_transport_error,
)
from katcha.config import get_settings

_TOKEN_URL = "https://www.reddit.com/api/v1/access_token"
_API_ROOT = "https://oauth.reddit.com"
_ALLOWED_SORTS = {"relevance", "hot", "top", "new", "comments"}
_ALLOWED_TIME_FILTERS = {"hour", "day", "week", "month", "year", "all"}


def _number(value: object) -> int:
    try:
        return max(int(float(str(value))), 0)
    except (TypeError, ValueError):
        return 0


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
        raise ValueError("reddit discovery requires q or include_terms")
    return " ".join(terms)


def _published_at(created_utc: object) -> str | None:
    try:
        timestamp = float(str(created_utc))
    except (TypeError, ValueError):
        return None
    if timestamp <= 0:
        return None
    return datetime.fromtimestamp(timestamp, tz=UTC).isoformat().replace("+00:00", "Z")


def parse_reddit_candidates(
    payload: dict[str, Any],
    *,
    freshness_horizon_hours: int,
    now: datetime | None = None,
) -> tuple[DiscoveredCandidate, ...]:
    reference = now or datetime.now(UTC)
    cutoff = reference - timedelta(hours=freshness_horizon_hours)
    data = payload.get("data")
    if not isinstance(data, dict):
        return ()
    children = data.get("children")
    if not isinstance(children, list):
        return ()

    candidates: list[DiscoveredCandidate] = []
    for rank, child in enumerate(children, start=1):
        if not isinstance(child, dict):
            continue
        raw = child.get("data")
        if not isinstance(raw, dict):
            continue
        fullname = str(raw.get("name") or "").strip()
        post_id = str(raw.get("id") or "").strip()
        if not fullname:
            fullname = f"t3_{post_id}" if post_id else ""
        if not fullname:
            continue

        published = _published_at(raw.get("created_utc"))
        if published:
            published_dt = datetime.fromisoformat(published.replace("Z", "+00:00"))
            if published_dt < cutoff:
                continue

        permalink = str(raw.get("permalink") or "").strip()
        if not permalink:
            continue
        source_url = f"https://www.reddit.com{permalink}"
        author = str(raw.get("author") or "").strip() or None
        subreddit = str(raw.get("subreddit") or "").strip() or None
        title = str(raw.get("title") or "").strip() or None
        outbound_url = str(raw.get("url_overridden_by_dest") or raw.get("url") or "").strip()
        metrics = {
            "score": _number(raw.get("score")),
            "upvotes": _number(raw.get("ups")),
            "comments": _number(raw.get("num_comments")),
            "search_rank": rank,
        }
        metadata: dict[str, Any] = {
            "reddit_post_id": post_id,
            "reddit_fullname": fullname,
            "subreddit": subreddit,
            "outbound_url": outbound_url or None,
            "domain": str(raw.get("domain") or "").strip() or None,
            "post_hint": str(raw.get("post_hint") or "").strip() or None,
            "is_video": bool(raw.get("is_video", False)),
            "over_18": bool(raw.get("over_18", False)),
            "upvote_ratio": float(raw.get("upvote_ratio") or 0.0),
            "search_rank": rank,
            "source_metrics": metrics,
        }
        if published:
            metadata["published_at"] = published

        candidates.append(
            DiscoveredCandidate(
                source_url=source_url,
                external_id=fullname,
                title=title,
                creator=author,
                creator_url=(
                    f"https://www.reddit.com/user/{author}/" if author else None
                ),
                provenance_confidence=0.9,
                provenance_claims={
                    "discovered_via": "reddit_oauth_api",
                    "reddit_post_id": post_id,
                    "reddit_fullname": fullname,
                    "subreddit": subreddit,
                },
                metadata=metadata,
            )
        )
    return tuple(candidates)


class RedditDiscoveryAdapter:
    key = "reddit"
    version = "v1"

    def __init__(self) -> None:
        self._token: str | None = None
        self._token_expires_at = 0.0

    def _access_token(self, client: httpx.Client) -> str:
        now = time.monotonic()
        if self._token and now < self._token_expires_at:
            return self._token

        settings = get_settings()
        if not settings.reddit_client_id or not settings.reddit_client_secret:
            raise ValueError("Reddit discovery is not configured")
        try:
            response = client.post(
                _TOKEN_URL,
                data={"grant_type": "client_credentials"},
                auth=(settings.reddit_client_id, settings.reddit_client_secret),
                headers={"User-Agent": settings.reddit_user_agent},
            )
        except httpx.HTTPError as exc:
            raise provider_transport_error(
                "Reddit",
                "OAuth token",
                provider_usage={"reddit.oauth": 1},
            ) from exc
        if response.status_code >= 400:
            raise provider_response_error(
                "Reddit",
                "OAuth token",
                response,
                provider_usage={"reddit.oauth": 1},
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise provider_payload_error(
                "Reddit",
                "OAuth token",
                provider_usage={"reddit.oauth": 1},
            ) from exc
        if not isinstance(payload, dict):
            raise provider_payload_error(
                "Reddit",
                "OAuth token",
                provider_usage={"reddit.oauth": 1},
            )
        token = str(payload.get("access_token") or "").strip()
        if not token:
            raise provider_payload_error("Reddit", "OAuth token")
        try:
            expires_in = max(int(payload.get("expires_in") or 3600), 60)
        except (TypeError, ValueError):
            expires_in = 3600
        self._token = token
        self._token_expires_at = now + max(expires_in - 60, 30)
        return token

    def discover(
        self,
        query: dict[str, Any],
        cursor: dict[str, Any],
    ) -> DiscoveryBatch:
        settings = get_settings()
        requested_limit = min(max(int(query.get("limit", 25)), 1), 100)
        sort = str(query.get("sort") or "new").strip().lower()
        if sort not in _ALLOWED_SORTS:
            raise ValueError(f"unsupported Reddit search sort: {sort}")
        time_filter = str(query.get("time_filter") or "week").strip().lower()
        if time_filter not in _ALLOWED_TIME_FILTERS:
            raise ValueError(f"unsupported Reddit time filter: {time_filter}")
        freshness_hours = min(
            max(int(query.get("freshness_horizon_hours", 72)), 1),
            24 * 365,
        )
        subreddit = str(query.get("subreddit") or "").strip().strip("/")
        after = str(cursor.get("after") or "").strip()
        path = f"/r/{subreddit}/search" if subreddit else "/search"
        params: dict[str, object] = {
            "q": _search_query(query),
            "sort": sort,
            "t": time_filter,
            "limit": requested_limit,
            "raw_json": 1,
        }
        if subreddit:
            params["restrict_sr"] = "on"
        if after:
            params["after"] = after

        with httpx.Client(timeout=15.0) as client:
            had_cached_token = bool(
                self._token and time.monotonic() < self._token_expires_at
            )
            token = self._access_token(client)
            try:
                response = client.get(
                    f"{_API_ROOT}{path}",
                    params=params,
                    headers={
                        "Authorization": f"bearer {token}",
                        "User-Agent": settings.reddit_user_agent,
                    },
                )
            except httpx.HTTPError as exc:
                usage = {"reddit.api": 1}
                if not had_cached_token:
                    usage["reddit.oauth"] = 1
                raise provider_transport_error(
                    "Reddit", "search", provider_usage=usage
                ) from exc
            if response.status_code >= 400:
                usage = {"reddit.api": 1}
                if not had_cached_token:
                    usage["reddit.oauth"] = 1
                raise provider_response_error(
                    "Reddit",
                    "search",
                    response,
                    provider_usage=usage,
                )
            try:
                payload = response.json()
            except ValueError as exc:
                usage = {"reddit.api": 1}
                if not had_cached_token:
                    usage["reddit.oauth"] = 1
                raise provider_payload_error(
                    "Reddit", "search", provider_usage=usage
                ) from exc
            if not isinstance(payload, dict):
                usage = {"reddit.api": 1}
                if not had_cached_token:
                    usage["reddit.oauth"] = 1
                raise provider_payload_error(
                    "Reddit", "search", provider_usage=usage
                )

        candidates = parse_reddit_candidates(
            payload,
            freshness_horizon_hours=freshness_hours,
        )
        data = payload.get("data")
        next_after = ""
        if isinstance(data, dict):
            next_after = str(data.get("after") or "").strip()
        usage = {"reddit.api": 1}
        if not had_cached_token:
            usage["reddit.oauth"] = 1
        return DiscoveryBatch(
            items=candidates,
            next_cursor={"after": next_after} if next_after else {},
            done=not bool(next_after),
            provider_usage=usage,
        )
