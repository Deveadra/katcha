from __future__ import annotations

import json
from decimal import Decimal
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx

from katcha.acquisition.adapters import (
    DiscoveredCandidate,
    DiscoveryAdapterCapability,
    DiscoveryBatch,
    DiscoveryProviderError,
)
from katcha.ai.pricing import estimate_token_cost
from katcha.ai.router import ModelTarget, assert_ai_budget, record_usage
from katcha.config import get_settings
from katcha.domain import AITask
from katcha.integrations.chatgpt import (
    ChatGPTConnectionError,
    invoke_web_search_json,
)
from katcha.integrations.codex import CodexConnectionError
from katcha.integrations.codex import invoke_web_search_json as invoke_codex_web_search_json

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
        if (candidate.netloc == source.netloc and candidate_path == source_path
                and candidate.query == source.query):
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


def _web_search_call_count(payload: dict[str, Any]) -> int:
    raw_output = payload.get("output")
    if not isinstance(raw_output, list):
        return 0
    return sum(
        1
        for item in raw_output
        if isinstance(item, dict) and item.get("type") == "web_search_call"
    )


def _response_output_text(payload: dict[str, Any]) -> str:
    chunks: list[str] = []
    raw_output = payload.get("output")
    if not isinstance(raw_output, list):
        return ""
    for item in raw_output:
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        content = item.get("content")
        if not isinstance(content, list):
            continue
        for part in content:
            if not isinstance(part, dict) or part.get("type") != "output_text":
                continue
            text = part.get("text")
            if isinstance(text, str):
                chunks.append(text)
    return "".join(chunks)


def _retry_after_seconds(response: httpx.Response) -> int | None:
    raw = response.headers.get("retry-after")
    if not raw:
        return None
    try:
        return max(int(float(raw)), 0)
    except ValueError:
        return None


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


def _recent_scout_sources(cursor: dict[str, Any]) -> list[str]:
    raw = cursor.get("recent_sources")
    if not isinstance(raw, list):
        return []
    values: list[str] = []
    for item in raw:
        normalized = _normalized_url(str(item or ""))
        if normalized and normalized not in values:
            values.append(normalized)
        if len(values) >= 60:
            break
    return values


def _next_scout_cursor(
    cursor: dict[str, Any],
    items: tuple[DiscoveredCandidate, ...],
) -> dict[str, Any]:
    recent = _recent_scout_sources(cursor)
    for item in items:
        for raw in (item.source_url, item.creator_url):
            normalized = _normalized_url(str(raw or ""))
            if normalized:
                if normalized in recent:
                    recent.remove(normalized)
                recent.append(normalized)
    return {
        "cycle": max(int(cursor.get("cycle") or 0), 0) + 1,
        "recent_sources": recent[-60:],
    }


def _search_prompt(
    query: dict[str, Any],
    limit: int,
    *,
    cursor: dict[str, Any] | None = None,
) -> str:
    include_terms = _clean_list(query.get("include_terms"), limit=30)
    exclude_terms = _clean_list(query.get("exclude_terms"), limit=30)
    platforms = _requested_platforms(query)
    operator_request = str(query.get("operator_request") or query.get("q") or "").strip()
    channel_context = str(query.get("channel_context") or "").strip()
    freshness_hours = min(max(int(query.get("freshness_horizon_hours", 72)), 1), 24 * 30)
    recent_sources = _recent_scout_sources(cursor or {})
    cycle = max(int((cursor or {}).get("cycle") or 0), 0)
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
        "Freshness target: prioritize material from the last "
        f"{freshness_hours} hours when possible.\n"
        f"Domain hints: {domain_hints or ['none; search the wider public web']}\n"
        f"Scout cycle: {cycle + 1}\n"
        f"Recently discovered sources to avoid simply repeating: {recent_sources[-30:]}\n"
        "Favor discovery breadth: relevant results may come from people or sites never seen "
        "before. Use recent sources as exploration memory: look for adjacent creators, linked "
        "communities, related sites, and newer posts rather than merely returning the same pages. "
        "Return source_kind as one of post, profile, community, website, feed, or video."
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
        required_credentials=(),
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
        settings = get_settings()
        if not settings.ai_enabled:
            raise ValueError("Autonomous web scouting requires KATCHA_AI_ENABLED=true")
        if settings.resolved_ai_execution_mode() != "live":
            raise ValueError(
                "Autonomous web scouting requires KATCHA_AI_EXECUTION_MODE=live"
            )
        limit = min(max(int(query.get("limit", 40)), 1), 100)

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
        plan_errors: list[str] = []
        providers = []
        if getattr(settings, "codex_enabled", False):
            providers.append(("codex", invoke_codex_web_search_json))
        if getattr(settings, "chatgpt_host_id", None):
            providers.append(("chatgpt", invoke_web_search_json))
        for provider, invoke in providers:
            try:
                plan = invoke(
                    prompt=_search_prompt(query, limit, cursor=cursor),
                    schema_name="katcha_web_scout_results",
                    schema=schema,
                    tool=_web_search_tool(query),
                    settings=settings,
                )
                response_payload = plan.payload
                web_search_calls = _web_search_call_count(response_payload)
                target = ModelTarget(provider, plan.model)
                record_usage(
                    task=AITask.METADATA,
                    target=target,
                    input_units=plan.input_tokens,
                    output_units=plan.output_tokens,
                    cost_usd=Decimal("0"),
                    reference_type="web_scout",
                    reference_id=str(response_payload.get("id") or ""),
                    metadata={
                        "web_search_calls": web_search_calls,
                        "chatgpt_plan": True,
                        "api_cost_usd": "0",
                    },
                )
                grounded_urls = _grounded_url_keys(response_payload)
                items = parse_web_scout_output(
                    getattr(plan, "text", "") or _response_output_text(response_payload),
                    grounded_urls=grounded_urls,
                    limit=limit,
                )
                return DiscoveryBatch(
                    items=items,
                    next_cursor=_next_scout_cursor(cursor, items),
                    done=True,
                    provider_usage={"openai.web_search": web_search_calls},
                )
            except (CodexConnectionError, ChatGPTConnectionError, httpx.HTTPError) as exc:
                plan_errors.append(f"{provider}: {exc}")

        if not (
            settings.openai_api_key
            and getattr(settings, "allow_paid_openai_fallback", False)
        ):
            if plan_errors:
                raise DiscoveryProviderError(
                    "Subscription web research is unavailable and paid OpenAI API "
                    "fallback is disabled or not configured. "
                    + " | ".join(plan_errors)[:1500],
                    kind="provider_unavailable",
                    transient=False,
                    provider_usage={"openai.web_search": 0},
                )
            raise ValueError(
                "Autonomous web scouting needs a connected Codex/ChatGPT plan. "
                "Paid OpenAI API fallback is opt-in from Katcha Settings."
            )

        target = ModelTarget("openai", settings.web_scout_model)
        estimated_cost = estimate_token_cost(target, 128000, 2200) + Decimal("0.01")
        assert_ai_budget(max(estimated_cost, Decimal("0.02")))

        request_payload = {
            "model": settings.web_scout_model,
            "input": _search_prompt(query, limit, cursor=cursor),
            "reasoning": {"effort": "low"},
            "tools": [_web_search_tool(query)],
            "tool_choice": "required",
            "include": ["web_search_call.action.sources"],
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "katcha_web_scout_results",
                    "schema": schema,
                    "strict": False,
                }
            },
            "max_output_tokens": 2200,
            "max_tool_calls": 1,
        }
        try:
            response = httpx.post(
                "https://api.openai.com/v1/responses",
                headers={
                    "Authorization": f"Bearer {settings.openai_api_key}",
                    "Content-Type": "application/json",
                },
                json=request_payload,
                timeout=90.0,
            )
        except httpx.HTTPError as exc:
            raise DiscoveryProviderError(
                "OpenAI web scout request failed before a response was received",
                kind="network_error",
                transient=True,
                provider_usage={"openai.web_search": 1},
            ) from exc

        if response.status_code >= 400:
            retry_after = _retry_after_seconds(response)
            rate_limited = response.status_code == 429
            transient = rate_limited or response.status_code >= 500
            raise DiscoveryProviderError(
                "OpenAI web scout request was rejected",
                kind="rate_limited" if rate_limited else "provider_http_error",
                transient=transient,
                status_code=response.status_code,
                retry_after_seconds=retry_after,
                provider_usage={"openai.web_search": 1},
            )
        try:
            response_payload = response.json()
        except ValueError as exc:
            raise DiscoveryProviderError(
                "OpenAI web scout returned a non-JSON response",
                kind="provider_payload",
                transient=True,
                provider_usage={"openai.web_search": 1},
            ) from exc
        if not isinstance(response_payload, dict):
            raise DiscoveryProviderError(
                "OpenAI web scout returned an unexpected response shape",
                kind="provider_payload",
                transient=False,
                provider_usage={"openai.web_search": 1},
            )

        web_search_calls = _web_search_call_count(response_payload)
        usage = response_payload.get("usage")
        usage_dict = usage if isinstance(usage, dict) else {}
        input_tokens = max(int(usage_dict.get("input_tokens") or 0), 0)
        output_tokens = max(int(usage_dict.get("output_tokens") or 0), 0)
        token_cost = estimate_token_cost(target, input_tokens, output_tokens)
        total_cost = token_cost + (Decimal("0.01") * web_search_calls)
        record_usage(
            task=AITask.METADATA,
            target=target,
            input_units=input_tokens,
            output_units=output_tokens,
            cost_usd=total_cost,
            reference_type="web_scout",
            reference_id=str(response_payload.get("id") or ""),
            metadata={
                "web_search_calls": web_search_calls,
                "web_search_tool_cost_usd": str(Decimal("0.01") * web_search_calls),
                "token_cost_usd": str(token_cost),
            },
        )

        grounded_urls = _grounded_url_keys(response_payload)
        items = parse_web_scout_output(
            _response_output_text(response_payload),
            grounded_urls=grounded_urls,
            limit=limit,
        )
        return DiscoveryBatch(
            items=items,
            next_cursor=_next_scout_cursor(cursor, items),
            done=True,
            provider_usage={"openai.web_search": web_search_calls},
        )
