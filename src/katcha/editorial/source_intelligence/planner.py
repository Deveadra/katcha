"""Map the installed discovery registry to bounded, deterministic search jobs."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from katcha.acquisition.adapters import DiscoveryAdapter, describe_adapter, get_adapter
from katcha.editorial.source_intelligence.contracts import (
    ProviderPolicyV1,
    SearchIntentV1,
    SourceSearchJobV1,
    digest,
)


@dataclass(frozen=True)
class SearchCapability:
    provider_key: str
    provider_version: str
    query_style: str | None
    page_limit: int
    external: bool = True
    potentially_paid: bool = False
    supports_negative_terms: bool = False
    availability: str = "reference_only"


def search_capability(adapter: DiscoveryAdapter) -> SearchCapability:
    """Only known adapter formats are dispatched; imports/feeds are not searches."""
    capability = describe_adapter(adapter)
    styles = {
        ("youtube", "v1"): ("youtube", 50, False, True),
        ("reddit", "v1"): ("reddit", 100, False, False),
        ("web_scout", "v1"): ("web_scout", 100, True, True),
    }
    style, limit, paid, negatives = styles.get(
        (capability.key, capability.version), (None, 0, False, False)
    )
    return SearchCapability(
        capability.key,
        capability.version,
        style,
        limit,
        potentially_paid=paid,
        supports_negative_terms=negatives,
    )


def plan_job(
    intent: SearchIntentV1,
    *,
    provider_key: str,
    provider_version: str,
    policy: ProviderPolicyV1,
    max_results: int = 20,
    capability: SearchCapability | None = None,
) -> SourceSearchJobV1:
    cap = capability or search_capability(get_adapter(provider_key, provider_version))
    if (cap.provider_key, cap.provider_version) != (provider_key, provider_version):
        raise ValueError("capability does not match provider")
    if cap.query_style is None:
        raise ValueError("adapter supports imports or feeds, not semantic search")
    if intent.negative_terms and not cap.supports_negative_terms:
        raise ValueError("adapter cannot preserve negative search terms")
    if isinstance(max_results, bool) or not 1 <= max_results <= cap.page_limit:
        raise ValueError("requested results exceed provider page limit")
    payload = {
        **intent.model_dump(mode="json"),
        "provider_key": provider_key,
        "provider_version": provider_version,
        "policy_version": policy.version,
        "cursor": {},
        "max_results": max_results,
        "max_cost": str(policy.call_cost_ceiling),
        "max_attempts": policy.max_attempts,
    }
    identity = digest(payload)
    return SourceSearchJobV1.model_validate(
        {
            **payload,
            "job_id": "ssj_" + identity,
            "idempotency_key": identity,
        }
    )


def next_page(job: SourceSearchJobV1, cursor: dict[str, Any]) -> SourceSearchJobV1:
    """New bounded page identity; callers use only a successful receipt's cursor."""
    payload = {**job.identity_payload(), "cursor": cursor}
    identity = digest(payload)
    result = SourceSearchJobV1.model_validate(
        {
            **payload,
            "job_id": "ssj_" + identity,
            "idempotency_key": identity,
        }
    )
    result.verify_identity()
    return result


def adapter_query(job: SourceSearchJobV1, cap: SearchCapability) -> dict[str, Any]:
    if cap.query_style == "youtube":
        # Avoid the legacy 72h recency default for historical script research.
        q = " ".join(job.query_terms) + "".join(
            " -" + '"' + term.replace('"', "") + '"' for term in job.negative_terms
        )
        return {
            "q": q,
            "limit": job.max_results,
            "order": "relevance",
            "freshness_horizon_hours": 0,
        }
    if cap.query_style == "reddit":
        # The legacy adapter caps recency at one year. Do not silently use its
        # one-week default; deeper historical scouting needs a different adapter.
        return {
            "q": " ".join(job.query_terms),
            "include_terms": list(job.query_terms),
            "limit": job.max_results,
            "sort": "relevance",
            "time_filter": "all",
            "freshness_horizon_hours": 24 * 365,
        }
    if cap.query_style in {"web_scout", "fixture"}:
        return {
            "q": " ".join(job.query_terms),
            "include_terms": list(job.query_terms),
            "exclude_terms": list(job.negative_terms),
            "limit": job.max_results,
        }
    raise ValueError("unsupported search query format")
