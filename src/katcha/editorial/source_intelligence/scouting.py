"""Bounded multi-provider pages using the existing dispatch and budget ledger."""

from __future__ import annotations

from dataclasses import dataclass

from katcha.acquisition.adapters import DiscoveryAdapter
from katcha.editorial.source_intelligence.candidates import AssetCandidateV1
from katcha.editorial.source_intelligence.contracts import (
    ProviderPolicyV1,
    Record,
    SearchReceiptV1,
    SearchScopeV1,
    SourceSearchJobV1,
)
from katcha.editorial.source_intelligence.jobs import SearchJobLedger
from katcha.editorial.source_intelligence.planner import SearchCapability, next_page


@dataclass(frozen=True)
class ScoutRequest:
    job: SourceSearchJobV1
    policy: ProviderPolicyV1
    adapter: DiscoveryAdapter | None = None
    capability: SearchCapability | None = None


class ScoutPageV1(Record):
    receipt: SearchReceiptV1
    candidates: tuple[AssetCandidateV1, ...]
    continuation: SourceSearchJobV1 | None = None
    gap_reason: str | None = None
    next_action: str | None = None


def scout(
    ledger: SearchJobLedger,
    requests: tuple[ScoutRequest, ...],
    *,
    scope: SearchScopeV1,
) -> tuple[ScoutPageV1, ...]:
    """Dispatch one page per job, at most 20 jobs, without automatic retries.

    Preflight the entire batch before side effects. Composition-root adapters
    and capabilities are trusted configuration, never client-supplied records.
    Continuations carry only saved successful cursors; scheduling them is an
    explicit bounded operation under the existing cumulative provider budget.
    """
    if not 1 <= len(requests) <= 20:
        raise ValueError("scouting requires between 1 and 20 jobs")
    for request in requests:
        if request.job.scope != scope:
            raise PermissionError("scouting job belongs to another scope")
        request.job.verify_identity()
        if request.adapter and (request.adapter.key, request.adapter.version) != (
            request.job.provider_key,
            request.job.provider_version,
        ):
            raise ValueError("adapter does not match job provider")
    pages = []
    for request in requests:
        receipt = ledger.dispatch(
            request.job,
            scope=scope,
            policy=request.policy,
            adapter=request.adapter,
            capability=request.capability,
        )
        candidates = ledger.candidates(request.job.job_id, scope=scope)
        continuation = None
        gap = None
        action = receipt.next_action
        if receipt.status == "succeeded":
            if not receipt.done:
                continuation = next_page(request.job, receipt.next_cursor)
            if not candidates:
                if receipt.source_urls:
                    gap = "legacy_metadata_not_retained"
                    action = "Inspect saved references; the old search did not retain metadata."
                else:
                    gap = "no_matches_on_page"
                    action = "Search the next saved page or choose another approved source."
        else:
            gap = receipt.error_category or receipt.status
            action = (
                action or "Wait for the active search or reconcile its outcome before retrying."
            )
        pages.append(
            ScoutPageV1(
                receipt=receipt,
                candidates=candidates,
                continuation=continuation,
                gap_reason=gap,
                next_action=action,
            )
        )
    return tuple(pages)
