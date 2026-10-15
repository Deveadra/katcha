"""Grounded Gemini discovery for connected channels, retaining provider source evidence."""

from __future__ import annotations

import uuid
from dataclasses import replace
from decimal import Decimal

from katcha.acquisition.adapters import DiscoveryBatch, DiscoveryProviderError
from katcha.ai.gemini_capacity import run_with_gemini_capacity_fallback
from katcha.ai.pricing import estimate_token_cost
from katcha.ai.router import (
    ModelTarget,
    assert_ai_budget,
    record_usage,
    release_budget_reservation,
    route_for_channel,
)
from katcha.domain import AITask
from katcha.runtime_fence import assert_mutation_authority


def discover_gemini(query, cursor, settings, limit):
    from google import genai

    from katcha.acquisition.web_scout import (
        _next_scout_cursor,
        _search_prompt,
        parse_web_scout_output,
    )

    target = ModelTarget("gemini", "gemini-3.8-flash")
    reservation = None
    estimate = Decimal("0.10")
    if query.get("channel_profile_id"):
        route = route_for_channel(
            AITask.METADATA,
            uuid.UUID(query["channel_profile_id"]),
            estimated_increment_usd=estimate,
            expected_value=0.5,
            preferred_target=target,
            reference_type="web_scout",
            reference_id=str(query.get("reservation_key") or ""),
            reservation_key=query.get("reservation_key"),
        )
        reservation = route.reservation_id
    else:
        assert_ai_budget(estimate)
    assert_mutation_authority("discovery.gemini_web_search", settings=settings)
    client = genai.Client(
        api_key=settings.gemini_api_key,
        http_options={"timeout": 45000, "retry_options": {"attempts": 1}},
    )
    try:

        def invoke(candidate):
            response = client.models.generate_content(
                model=candidate.model,
                contents=_search_prompt(query, limit, cursor=cursor),
                config={
                    "tools": [{"google_search": {}}],
                    "thinking_config": {"thinking_level": "low"},
                    "max_output_tokens": 2400,
                },
            )
            return response, candidate

        response, target = run_with_gemini_capacity_fallback(target, invoke)
        usage = response.usage_metadata
        inputs = int(getattr(usage, "prompt_token_count", 0) or 0)
        outputs = int(getattr(usage, "candidates_token_count", 0) or 0) + int(
            getattr(usage, "thoughts_token_count", 0) or 0
        )
        sources = {}
        search_queries = set()
        for candidate in response.candidates or []:
            metadata = getattr(candidate, "grounding_metadata", None)
            search_queries.update(
                value.strip()
                for value in (getattr(metadata, "web_search_queries", None) or [])
                if isinstance(value, str) and value.strip()
            )
            for chunk in getattr(metadata, "grounding_chunks", None) or []:
                web = getattr(chunk, "web", None)
                url = getattr(web, "uri", None)
                if url and url.startswith("https://"):
                    sources[url] = getattr(web, "title", None)
        search_count = len(search_queries) or int(bool(sources))
        # Paid grounding rate, conservatively ignoring free tier credits.
        # https://ai.google.dev/gemini-api/docs/google-search/#pricing (2026-10-15)
        search_cost = Decimal("0.014") * search_count
        record_usage(
            task=AITask.METADATA,
            target=target,
            input_units=inputs,
            output_units=outputs,
            cost_usd=estimate_token_cost(target, inputs, outputs) + search_cost,
            reference_type="web_scout",
            reference_id=str(query.get("reservation_key") or ""),
            reservation_id=reservation,
            metadata={
                "grounded_search": True,
                "search_queries": search_count,
                "search_cost_usd": str(search_cost),
                "search_count_estimated": bool(sources and not search_queries),
            },
        )
        try:
            items = parse_web_scout_output(
                response.text or "", grounded_urls=set(sources), limit=limit
            )
        except DiscoveryProviderError:
            if not sources:
                raise
            items = ()
        if not items and sources:
            # Grounding URIs may be provider redirects absent from the answer JSON.
            # Keep those observed URIs instead of accepting ungrounded model URLs.
            import json

            items = parse_web_scout_output(
                json.dumps(
                    {
                        "items": [
                            {"source_url": url, "title": title} for url, title in sources.items()
                        ]
                    }
                ),
                grounded_urls=set(sources),
                limit=limit,
            )
        items = tuple(
            replace(
                item,
                provenance_claims={
                    **item.provenance_claims,
                    "discovered_via": "gemini_google_search",
                },
            )
            for item in items
        )
        return DiscoveryBatch(
            items=items,
            next_cursor=_next_scout_cursor(cursor, items),
            done=True,
            provider_usage={"gemini.web_search": search_count},
        )
    except Exception:
        release_budget_reservation(reservation, reason="gemini_web_scout_failed")
        raise
    finally:
        client.close()
