"""Run the explicitly offline KSI-102 acceptance fixture; never contacts a provider."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any
from uuid import UUID

from katcha.acquisition.adapters import DiscoveredCandidate, DiscoveryBatch
from katcha.editorial.source_intelligence.contracts import (
    ProviderPolicyV1,
    SearchIntentV1,
    SearchScopeV1,
    digest,
)
from katcha.editorial.source_intelligence.jobs import SearchJobLedger
from katcha.editorial.source_intelligence.planner import SearchCapability, plan_job


class FixtureSearchAdapter:
    key = "fixture_search"
    version = "v1"

    def __init__(self) -> None:
        self.calls = 0

    def discover(self, query: dict[str, Any], cursor: dict[str, Any]) -> DiscoveryBatch:
        self.calls += 1
        return DiscoveryBatch(
            items=(
                DiscoveredCandidate(
                    "https://example.org/fixture/" + digest(query),
                    metadata={"fixture_only": True},
                ),
            ),
            provider_usage={"fixture.search": 1},
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", action="store_true", required=True)
    parser.add_argument("--ledger", type=Path, required=True)
    args = parser.parse_args()
    adapter = FixtureSearchAdapter()
    cap = SearchCapability(
        adapter.key, adapter.version, "fixture", 100, external=False, supports_negative_terms=True
    )
    scope = SearchScopeV1(channel_id=UUID(int=1), project_id=UUID(int=2), revision_digest="a" * 64)
    policy = ProviderPolicyV1(version="fixture-policy-v1", max_calls=120, max_results=240)
    ledger = SearchJobLedger(args.ledger)
    receipts = []
    for i in range(120):
        intent = SearchIntentV1(
            scope=scope,
            requirement_id=f"fixture-req-{i}",
            query_terms=(f"visual requirement {i}",),
            semantic_brief="Synthetic fixture requirement; no script or scene observations.",
            source_purpose="official_reference",
            requested_by="offline-fixture",
        )
        job = plan_job(
            intent,
            provider_key=adapter.key,
            provider_version=adapter.version,
            policy=policy,
            max_results=2,
            capability=cap,
        )
        receipt = ledger.dispatch(job, scope=scope, policy=policy, adapter=adapter, capability=cap)
        receipts.append(receipt.model_dump(mode="json"))
    print(
        json.dumps(
            {
                "evidence_grade": "fixture_only",
                "real_provider_calls": 0,
                "fixture_calls_this_run": adapter.calls,
                "requirements": 120,
                "successful_search_jobs": sum(r["status"] == "succeeded" for r in receipts),
                "verified_usable_assets": 0,
                "receipts": receipts,
            },
            sort_keys=True,
        )
    )
    return 0 if all(r["status"] == "succeeded" for r in receipts) else 1


if __name__ == "__main__":
    raise SystemExit(main())
