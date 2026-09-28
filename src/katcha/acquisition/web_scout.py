from __future__ import annotations

import json
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from katcha.acquisition.adapters import (
    DiscoveryAdapterCapability,
    DiscoveryBatch,
    DiscoveredCandidate,
    DiscoveryProviderError,
)
from katcha.config import get_settings

_PLATFORM_DOMAINS = {
    "tiktok": ("tiktok.com",),
    "instagram": ("instagram.com",),
    "x": ("x.com", "twitter.com"),
    "bluesky": ("bsky.app",),
    "youtube": ("youtube.com", "youtu.be"),
    "reddit": ("reddit.com",),
    "discord": ("discord.com", "discord.gg"),
}


def _normalized_url(value: str) -> str:
    raw = value.strip()
    if not raw:
        return ""
    try:
        parts = urlsplit(raw)
    except ValueError:
        return ""
    if parts.scheme not in {"http", "https"} or not parts.netloc:
        return ""
    host = parts.netloc.casefold()
    if host.startswith("www."):
        host = host[4:]
    path = parts.path.rstrip("/") or "/"
    return urlunsplit((parts.scheme.casefold(), host, path, parts.query, ""))


def _grounded_url_keys(response_payload: dict[str, Any]) -> set[str]:
    urls: set[str] = set()

    def walk(value: object, *, in_web_search: bool = False) -> None:
        if isinstance(value, dict):
            kind = str(value.get("type") or "")
            active = in_web_search or kind == "web_search_call"
            if kind == "url_citation":
                normalized = _normalized_url(str(value.get("url") or ""))
                if normalized:
                    urls.add(normalized)
            if active:
                normalized = _normalized_url(str(value.get("url") or ""))
                if normalized:
                    urls.add(normalized)
            for child in value.values():
                walk(child, in_web_search=active)
        elif isinstance(value, list):
            for child in value:
                walk(child, in_web_search=in_web_search)

    walk(response_payload)
    return urls


def _same_grounded_page(url: str, grounded: set[str]) -> bool:
    normalized = _normalized_url(url)
    if not normalized:
        return False
    if normalized in grounded:
        return True
    try:
        candidate = urlsplit(normalized)
    except ValueError:
        return False
    candidate_path = candidate.path.rstrip("/") or "/"
    for raw in grounded:
        try:
            source = urlsplit(raw)
        except ValueError:
            continue
        source_path = source.path.rstrip("/") or "/"
        if candidate.netloc == source.netloc and candidate_path == source_path:
            return True
    return False


def _platform_for_url(url: str) -> str:
    try:
        host = urlsplit(url).netloc.casefold()
    except ValueError:
        return "web"
    if host.startswith("www."):
        host = host[4:]
    for platform, domains in _PLATFORM_DOMAINS.items():
        if any(host == domain or host.endswith("." + domain) for domain in domains):
            return platform
    return "web"


def parse_web_scout_output(
    output_text: str,
    *,
    grounded_urls: set[str],
    limit: int,
) -> tuple[DiscoveredCandidate, ...]:
    try:
        payload = json.loads(output_text)
    except json.JSONDecodeError as exc:
        raise DiscoveryProviderError(
            "OpenAI web scout returned invalid JSON",
            kind="provider_payload",
            transient=False,
            provider_usage={"openai.web_search": 1},
        ) from exc
    raw_items = payload.get("items") if isinstance(payload, dict) else None
    if not isinstance(raw_items, list):
        raise DiscoveryProviderError(
            "OpenAI web scout response is missing items",
            kind="provider_payload",
            transient=False,
            provider_usage={"openai.web_search": 1},
        )

    items: list[DiscoveredCandidate] = []
    seen: set[str] = set()
    for raw in raw_items:
        if not isinstance(raw, dict):
            continue
        source_url = _normalized_url(str(raw.get("source_url") or ""))
        if not source_url or source_url in seen:
            continue
        if not _same_grounded_page(source_url, grounded_urls):
            continue
        seen.add(source_url)
        items.append(
            DiscoveredCandidate(
                source_url=source_url,
                title=str(raw.get("title") or "").strip() or None,
                creator=str(raw.get("creator") or "").strip() or None,
                creator_url=(
                    _normalized_url(str(raw.get("creator_url") or "")) or None
                ),
                provenance_confidence=0.86,
                provenance_claims={
                    "discovered_via": "openai_web_search",
                    "grounded_search_result": True,
                },
                metadata={
                    "platform": _platform_for_url(source_url),
                    "summary": str(raw.get("summary") or "").strip() or None,
                    "why_relevant": str(raw.get("why_relevant") or "").strip() or None,
                    "source_kind": str(raw.get("source_kind") or "content").strip(),
                },
            )
        )
        if len(items) >= limit:
            break
    return tuple(items)


def _clean_list(value: object, *, limit: int = 20) -> list[str]:
    if not isinstance(value, list):
        return []
    result: list[str] = []
    for raw in value:
        item = str(raw or "").strip()
        if item and item.casefold() not in {entry.casefold() for entry in result}:
            result.append(item)
        if len(result) >= limit:
            break
    return result


def _requested_platforms(query: dict[str, Any]) -> list[str]:
    return [
        value.casefold()
        for value in _clean_list(query.get("platforms"), limit=12)
        if value.casefold() in _PLATFORM_DOMAINS
    ]


def _web_search_tool(query: dict[str, Any]) -> dict[str, Any]:
    platforms = _requested_platforms(query)
    domains = sorted(
        {
            domain
            for platform in platforms
            for domain in _PLATFORM_DOMAINS.get(platform, ())
        }
    )
    tool: dict[str, Any] = {"type": "web_search"}
    if domains:
        tool["filters"] = {"allowed_domains": domains}
    return tool


def _search_prompt(query: dict[str, Any], limit: int) -> str:
    include_terms = _clean_list(query.get("include_terms"), limit=30)
    exclude_terms = _clean_list(query.get("exclude_terms"), limit=30)
    platforms = _requested_platforms(query)
    operator_request = str(query.get("operator_request") or query.get("q") or "").strip()
    channel_context = str(query.get("channel_context") or "").strip()
    freshness_hours = min(max(int(query.get("freshness_horizon_hours", 72)), 1), 24 * 30)
    domain_hints = sorted(
        {
            domain
            for platform in platforms
            for domain in _PLATFORM_DOMAINS.get(platform, ())
        }
    )
    return (
        "Scout the public web for fresh content sources and individual posts that could feed "
        "a YouTube channel's discovery pipeline. Search beyond already-known creators. Prefer "
        "recent, active sources with concrete post/page URLs. It is useful to surface a new "
        "website, account, community, or creator when it repeatedly publishes relevant material. "
        "Do not invent URLs. Only return URLs present in your web search results. "
        f"Return at most {limit} items.\n\n"
        f"Operator request: {operator_request or 'Find new relevant sources.'}\n"
        f"Channel context: {channel_context or 'No additional channel context was supplied.'}\n"
        f"Include terms: {include_terms}\n"
        f"Exclude terms: {exclude_terms}\n"
        f"Requested platforms: {platforms or ['open web']}\n"
        f"Freshness target: prioritize material from the last {freshness_hours} hours when possible.\n"
        f"Domain hints: {domain_hints or ['none; search the wider public web']}\n"
        "Favor discovery breadth: relevant results may come from people or sites never seen "
        "before. Return source_kind as one of post, profile, community, website, feed, or video."
    )


class WebScoutDiscoveryAdapter:
    key = "web_scout"
    version = "v1"
    capability = DiscoveryAdapterCapability(
        key=key,
        version=version,
        label="Autonomous Web Scout",
        description=(
            "Uses grounded web search to discover public posts, creators, communities, and "
            "new websites without requiring each profile or domain to be manually registered."
        ),
        source_types=("web_search", "source_scout", "social_search", "site_discovery"),
        supported_platforms=(
            "web",
            "tiktok",
            "instagram",
            "x",
            "bluesky",
            "youtube",
            "reddit",
            "discord",
        ),
        query_fields=(
            "q",
            "operator_request",
            "channel_context",
            "include_terms",
            "exclude_terms",
            "platforms",
            "freshness_horizon_hours",
            "limit",
        ),
        required_credentials=("KATCHA_OPENAI_API_KEY",),
        sample_query={
            "operator_request": "Find new gaming clip sources and communities.",
            "platforms": ["tiktok", "instagram", "x", "bluesky"],
            "include_terms": ["gaming"],
            "freshness_horizon_hours": 72,
            "limit": 40,
        },
    )

    def discover(
        self,
        query: dict[str, Any],
        cursor: dict[str, Any],
    ) -> DiscoveryBatch:
        del cursor
        settings = get_settings()
        if not settings.openai_api_key:
            raise ValueError("Autonomous web scouting requires KATCHA_OPENAI_API_KEY")
        limit = min(max(int(query.get("limit", 40)), 1), 100)

        from openai import OpenAI

        schema = {
            "type": "object",
            "properties": {
                "items": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "source_url": {"type": "string"},
                            "title": {"type": ["string", "null"]},
                            "creator": {"type": ["string", "null"]},
                            "creator_url": {"type": ["string", "null"]},
                            "summary": {"type": ["string", "null"]},
                            "why_relevant": {"type": ["string", "null"]},
                            "source_kind": {"type": ["string", "null"]},
                        },
                        "required": ["source_url"],
                        "additionalProperties": False,
                    },
                }
            },
            "required": ["items"],
            "additionalProperties": False,
        }
        try:
            response = OpenAI(api_key=settings.openai_api_key).responses.create(
                model=settings.web_scout_model,
                input=_search_prompt(query, limit),
                reasoning={"effort": "low"},
                tools=[_web_search_tool(query)],
                tool_choice="required",
                include=["web_search_call.action.sources"],
                text={
                    "format": {
                        "type": "json_schema",
                        "name": "katcha_web_scout_results",
                        "schema": schema,
                        "strict": False,
                    }
                },
                max_output_tokens=2200,
            )
        except Exception as exc:
            raise DiscoveryProviderError(
                "OpenAI web scout request failed",
                kind="provider_error",
                transient=True,
                provider_usage={"openai.web_search": 1},
            ) from exc

        response_payload = response.model_dump()
        grounded_urls = _grounded_url_keys(response_payload)
        items = parse_web_scout_output(
            response.output_text,
            grounded_urls=grounded_urls,
            limit=limit,
        )
        return DiscoveryBatch(
            items=items,
            done=True,
            provider_usage={"openai.web_search": 1},
        )
