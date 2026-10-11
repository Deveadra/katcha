"""Offline lexical references and honest coverage; fixtures are never usable media."""

import hashlib
from uuid import UUID

import httpx
import pytest
from pydantic import ValidationError

from katcha.acquisition.adapters import DiscoveredCandidate, DiscoveryBatch, DiscoveryProviderError
from katcha.editorial.project_schemas import EditorialScriptSeed
from katcha.editorial.source_intelligence.candidate_ranker import (
    SourceCoverageReportV1,
    rank_source_coverage,
)
from katcha.editorial.source_intelligence.contracts import ProviderPolicyV1
from katcha.editorial.source_intelligence.jobs import SearchJobLedger
from katcha.editorial.source_intelligence.planner import SearchCapability
from katcha.editorial.source_intelligence.query_expansion import (
    ProviderRoute,
    QueryHintV1,
    expand_queries,
)
from katcha.editorial.source_intelligence.requirements import build_asset_manifest
from katcha.editorial.source_intelligence.scouting import ScoutPageV1, ScoutRequest, scout


@pytest.fixture(autouse=True)
def no_network(monkeypatch, tmp_path):
    global LEDGER
    LEDGER = SearchJobLedger(tmp_path / "saved.sqlite")

    def reject(*args, **kwargs):
        pytest.fail("real network prohibited")

    monkeypatch.setattr(httpx.Client, "request", reject)


def manifest(text="[VISUAL: Vision at a window]\nNarration.", channel=1):
    return build_asset_manifest(
        EditorialScriptSeed(
            text=text,
            origin="operator_paste",
            content_sha256=hashlib.sha256(text.encode()).hexdigest(),
        ),
        channel_id=UUID(int=channel),
        project_id=UUID(int=2),
    )


def jobs(source, providers=("first",), hints=None):
    routes = tuple(
        ProviderRoute(
            SearchCapability(key, "v1", "fixture", 100, external=False),
            ProviderPolicyV1(version="fixture-v1", max_calls=1000, max_results=10000),
            "owned",
            max_results=10,
        )
        for key in providers
    )
    return expand_queries(source, routes=routes, hints=hints, requested_by="fixture").jobs


class FixtureAdapter:
    version = "v1"

    def __init__(self, key, items=(), error=None):
        self.key = key
        self.items = items
        self.error = error

    def discover(self, query, cursor):
        if self.error:
            raise self.error
        return DiscoveryBatch(items=self.items)


def page(
    job,
    *,
    title="Vision window",
    url="https://publisher.example/vision",
    description=None,
    extra=(),
):
    item = DiscoveredCandidate(
        url,
        title=title,
        creator="Official studio",
        metadata={"description": description} if description is not None else {},
    )
    request = ScoutRequest(
        job,
        ProviderPolicyV1(version="fixture-v1", max_calls=1000, max_results=10000),
        FixtureAdapter(job.provider_key, (item,) + extra),
        SearchCapability(job.provider_key, "v1", "fixture", 100, external=False),
    )
    return scout(LEDGER, (request,), scope=job.scope)[0]


def report(source, planned, pages, **kwargs):
    return rank_source_coverage(source, ledger=LEDGER, jobs=planned, pages=pages, **kwargs)


def test_120_cues_complete_deterministic_reference_coverage_no_usable_media():
    source = manifest("\n".join(f"[IMAGE: evidence diagram {number}]" for number in range(120)))
    planned = jobs(source)
    pages = tuple(
        page(job, title=f"evidence diagram {number}", url=f"https://publisher.example/{number}")
        for number, job in enumerate(planned)
    )
    result = report(source, planned, pages)
    assert len(result.requirements) == 120
    assert all(row.coverage_status == "reference_only" for row in result.requirements)
    assert all(row.usable_media_count == 0 for row in result.requirements)
    assert result.usable_media_count == 0
    assert all(row.references[0].rights_state == "not_assessed" for row in result.requirements)
    assert all(row.references[0].publisher_authority == "unverified" for row in result.requirements)
    assert all(row.references[0].quality is None for row in result.requirements)
    assert all(row.references[0].source_in_seconds is None for row in result.requirements)
    assert result == report(source, tuple(reversed(planned)), tuple(reversed(pages)))
    assert result == report(source, planned, pages + pages)
    assert SourceCoverageReportV1.model_validate_json(result.model_dump_json()) == result
    assert source.plan.source_text == manifest(source.plan.source_text).plan.source_text


def test_duplicate_canonical_urls_merge_provenance_and_diversify_providers():
    source = manifest()
    planned = jobs(source, ("first", "second", "third"))
    first = page(planned[0], url="https://publisher.example/vision#one")
    second = page(planned[1], url="https://publisher.example/vision#two")
    third = page(planned[2], title="Vision", url="https://different.example/vision")
    result = report(source, planned, (first, second, third), max_references=2)
    row = result.requirements[0]
    assert row.relevant_reference_count == 2
    assert len(row.references) == 2
    combined = row.references[0]
    assert combined.supporting_providers == ("first", "second")
    assert len(combined.scout_receipt_refs) == len(combined.observation_receipt_refs) == 2
    assert combined.canonical_url == "https://publisher.example/vision"
    assert row.references[1].provider == "third"
    assert row.references[1].canonical_url != combined.canonical_url


def test_diversity_selects_other_provider_before_second_same_provider_result():
    source = manifest()
    planned = jobs(source, ("first", "second"))
    first = page(
        planned[0],
        extra=(
            DiscoveredCandidate(
                "https://publisher.example/other",
                title="Vision window",
            ),
        ),
    )
    second = page(planned[1], title="Vision", url="https://second.example/vision")
    result = report(source, planned, (first, second), max_references=2)
    assert [item.provider for item in result.requirements[0].references] == ["first", "second"]
    assert result.requirements[0].relevant_reference_count == 3


def test_blank_irrelevant_evidence_and_ambiguous_narration_remain_gaps():
    source = manifest()
    planned = jobs(source, ("blank", "irrelevant"))
    result = report(
        source,
        planned,
        (
            page(planned[0], title=None),
            page(planned[1], title="Gardening compost"),
        ),
    )
    row, ambiguous = result.requirements
    assert row.coverage_status == "unresolved" and not row.references
    assert row.rejected_candidate_count == 2
    assert any(gap.category == "irrelevant_metadata" for gap in row.gaps)
    assert ambiguous.coverage_status == "needs_operator_input"
    assert ambiguous.gaps[0].category == "visual_purpose_unknown"
    assert result.usable_media_count == 0


def test_description_is_provider_reported_not_scene_or_authority_evidence():
    source = manifest()
    planned = jobs(source)
    result = report(
        source, planned, (page(planned[0], title="Official studio", description="Vision window"),)
    )
    reference = result.requirements[0].references[0]
    assert reference.matched_title_terms == ()
    assert reference.matched_description_terms == ("vision", "window")
    assert reference.observation_basis == "provider_reported"
    assert reference.publisher_authority == "unverified"
    assert reference.rights_state == "not_assessed"


def test_alternative_only_match_is_labeled_unverified_and_does_not_resolve_ambiguity():
    source = manifest("Narration without a visual.")
    req = source.requirements[0]
    planned = jobs(
        source,
        hints={req.requirement_id: (QueryHintV1(kind="alternative", value="timeline diagram"),)},
    )
    result = report(source, planned, (page(planned[0], title="timeline diagram"),))
    (row,) = result.requirements
    assert row.coverage_status == "needs_operator_input"
    assert row.references[0].relevance_basis == "unverified_query_metadata_overlap"
    assert row.usable_media_count == 0


def test_failed_provider_preserves_receipt_action_and_does_not_hide_healthy_reference(tmp_path):

    source = manifest()
    planned = jobs(source, ("failed", "healthy"))

    class FixtureAdapter:
        version = "v1"

        def __init__(self, key):
            self.key = key

        def discover(self, query, cursor):
            if self.key == "failed":
                raise TimeoutError("PRIVATE")
            return DiscoveryBatch(
                items=(
                    DiscoveredCandidate("https://publisher.example/vision", title="Vision window"),
                )
            )

    requests = tuple(
        ScoutRequest(
            job,
            ProviderPolicyV1(version="fixture-v1", max_calls=1000, max_results=10000),
            FixtureAdapter(job.provider_key),
            SearchCapability(job.provider_key, "v1", "fixture", 100, external=False),
        )
        for job in planned
    )
    pages = scout(LEDGER, requests, scope=source.scope)
    result = report(source, planned, pages)
    row = result.requirements[0]
    assert row.coverage_status == "reference_only"
    assert len(row.references) == 1
    failure = next(gap for gap in row.gaps if gap.category == pages[0].receipt.error_category)
    assert failure.receipt_refs == (pages[0].receipt.receipt_id,)
    assert failure.next_action == pages[0].receipt.next_action
    assert "PRIVATE" not in result.model_dump_json()


@pytest.mark.parametrize("status", ["failed", "policy_blocked", "quota_exceeded", "rate_limited"])
def test_only_failed_or_blocked_providers_yield_explicit_blocked_coverage(status):
    source = manifest()
    planned = jobs(source)
    cap = SearchCapability(
        planned[0].provider_key,
        "v1",
        "fixture",
        100,
        external=False,
        potentially_paid=status == "policy_blocked",
    )
    policy = ProviderPolicyV1(
        version="fixture-v1", max_calls=1000, max_results=1 if status == "quota_exceeded" else 10000
    )
    error = (
        DiscoveryProviderError("Fixture refusal", kind="rate_limited", status_code=429)
        if status == "rate_limited"
        else DiscoveryProviderError("Fixture refusal", kind="forbidden", status_code=403)
    )
    saved = scout(
        LEDGER,
        (
            ScoutRequest(
                planned[0], policy, FixtureAdapter(planned[0].provider_key, error=error), cap
            ),
        ),
        scope=source.scope,
    )[0]
    result = report(source, planned, (saved,))
    row = result.requirements[0]
    assert row.coverage_status == "blocked"
    failure_gap = next(gap for gap in row.gaps if gap.receipt_refs)
    assert failure_gap.receipt_refs == (saved.receipt.receipt_id,)
    assert failure_gap.next_action == saved.receipt.next_action
    assert row.usable_media_count == 0


def test_cross_channel_fails_closed_and_stale_revision_returns_actionable_rejection():
    source = manifest()
    planned = jobs(source)
    saved = page(planned[0])
    with pytest.raises(PermissionError, match="channel/project"):
        report(manifest(channel=999), planned, (saved,))
    changed = manifest("[VISUAL: Vision at a new window]\nNarration.")
    result = report(changed, planned, (saved,))
    assert any(item.category == "stale_revision" for item in result.rejected_evidence)
    assert any(
        item.receipt_refs == (saved.receipt.receipt_id,) for item in result.rejected_evidence
    )
    assert not any(row.references for row in result.requirements)


@pytest.mark.parametrize(
    "mutation", ["publisher", "provider", "observation", "scout", "url", "requirement"]
)
def test_unbacked_or_corrupt_candidate_evidence_never_ranked(mutation):
    source = manifest()
    planned = jobs(source)
    saved = page(planned[0])
    candidate = saved.candidates[0]
    updates = {
        "publisher": {"publisher": "Invented verified publisher"},
        "provider": {"provider": "other"},
        "observation": {"provenance_evidence_refs": ("fake-observation",)},
        "scout": {"scout_receipts": ("fake-receipt",)},
        "url": {"canonical_url": "https://different.example/fake"},
        "requirement": {"requirement_ids": ("unrelated",)},
    }
    candidate = candidate.model_copy(update=updates[mutation])
    saved = saved.model_copy(update={"candidates": (candidate,)})
    result = report(source, planned, (saved,))
    row = result.requirements[0]
    assert not row.references
    assert result.rejected_evidence[0].category == "saved_page_mismatch"
    assert row.rejected_candidate_count == 1


def test_conflicting_receipts_unbacked_receipts_and_incomplete_success_rejected():
    source = manifest()
    planned = jobs(source)
    saved = page(planned[0])
    conflict = saved.model_copy(update={"candidates": ()})
    result = report(source, planned, (saved, conflict))
    assert result.rejected_evidence[0].category == "conflicting_receipt_evidence"
    assert not result.requirements[0].references
    result = report(source, (), (saved,))
    assert result.rejected_evidence[0].category == "unbacked_receipt"
    incomplete = saved.model_copy(
        update={
            "receipt": saved.receipt.model_copy(update={"finished_at": None}),
        }
    )
    result = report(source, planned, (incomplete,))
    assert result.rejected_evidence[0].category == "saved_page_mismatch"
    assert not result.requirements[0].references


def test_reference_bounds_and_contract_reject_usable_media_claims():
    source = manifest()
    with pytest.raises(ValueError, match="reference bound"):
        report(source, (), (), max_references=True)
    value = report(source, (), ()).model_dump()
    value["usable_media_count"] = 1
    with pytest.raises(ValidationError):
        SourceCoverageReportV1.model_validate(value)


def test_coherently_tampered_title_or_forged_success_cannot_replace_saved_evidence():
    source = manifest()
    planned = jobs(source)
    saved = page(planned[0], title="Gardening compost")
    candidate = saved.candidates[0]
    candidate = candidate.model_copy(
        update={
            "observed_metadata": candidate.observed_metadata.model_copy(
                update={"title": "Vision window"}
            ),
        }
    )
    forged = saved.model_copy(update={"candidates": (candidate,)})
    result = report(source, planned, (forged,))
    assert result.rejected_evidence[0].category == "saved_page_mismatch"
    assert not result.requirements[0].references
    # A caller cannot forge a successful receipt for an unrecorded job.
    other = jobs(source, ("unrecorded",))[0]
    from katcha.editorial.source_intelligence.contracts import digest

    forged = saved.model_copy(
        update={
            "receipt": saved.receipt.model_copy(
                update={
                    "job_id": other.job_id,
                    "receipt_id": "ssr_" + digest([other.job_id, 1]),
                }
            ),
        }
    )
    result = report(source, (other,), (forged,))
    assert result.rejected_evidence[0].category == "saved_page_mismatch"
    assert result.usable_media_count == 0


def test_long_unicode_terms_long_gap_and_saved_recovery_display_remain_bounded():
    source = manifest("[VISUAL: " + "ß" * 300 + "]")
    planned = jobs(source)
    result = report(source, planned, (page(planned[0], title="ß" * 300),))
    assert result.requirements[0].references[0].matched_title_terms == ("ss" * 300,)
    ambiguous = manifest("Narration.")
    req = ambiguous.requirements[0].model_copy(update={"gap_reason": "g" * 2000})
    ambiguous = ambiguous.model_copy(update={"requirements": (req,)})
    result = report(ambiguous, (), ())
    assert len(result.requirements[0].gaps[0].next_action) == 1000
    assert ambiguous.requirements[0].gap_reason == "g" * 2000
    # Seed a legacy long recovery message as durable fixture evidence.
    saved = LEDGER.read(planned[0].job_id, scope=source.scope)
    receipt = LEDGER._finish(
        saved, status="failed", next_action="n" * 2000, error_category="e" * 500
    )
    saved_page = ScoutPageV1(
        receipt=receipt, candidates=LEDGER.candidates(planned[0].job_id, scope=source.scope)
    )
    result = report(source, planned, (saved_page,))
    gap = result.requirements[0].gaps[0]
    assert len(gap.category) == 200
    assert len(gap.next_action) == 1000
    assert LEDGER.read(planned[0].job_id, scope=source.scope).next_action == "n" * 2000


def test_jobs_without_pages_report_stale_invalid_or_wrong_requirement_intent():
    source = manifest()
    planned = jobs(source)
    stale = report(manifest("[VISUAL: Changed scene]"), planned, ())
    assert stale.rejected_evidence[0].category == "stale_revision_job"
    bad = planned[0].model_copy(update={"semantic_brief": "Changed intent"})
    result = report(source, (bad,), ())
    assert result.rejected_evidence[0].category == "invalid_job_identity"
    assert result.rejected_evidence[0].job_refs == (bad.job_id,)


def test_forged_page_gap_does_not_override_saved_successful_evidence():
    source = manifest()
    planned = jobs(source)
    saved = page(planned[0])
    forged_display = saved.model_copy(
        update={
            "gap_reason": "production_cleared",
            "next_action": "Use this media immediately",
        }
    )
    result = report(source, planned, (forged_display,))
    assert result == report(source, planned, (saved,))
    assert result.requirements[0].coverage_status == "reference_only"


def test_overlong_forged_receipt_rejected_without_echo_or_report_failure():
    source = manifest()
    planned = jobs(source)
    saved = page(planned[0])
    forged = saved.model_copy(
        update={
            "receipt": saved.receipt.model_copy(update={"receipt_id": "x" * 1000}),
        }
    )
    result = report(source, planned, (forged,))
    assert result.rejected_evidence[0].category == "invalid_receipt_identity"
    assert result.rejected_evidence[0].receipt_refs == ()
    assert len(result.requirements) == len(source.requirements)
    assert result.usable_media_count == 0


def test_rejected_evidence_and_candidate_order_are_deterministic():
    source = manifest()
    planned = jobs(source, ("first", "second"))
    saved = page(
        planned[0],
        title="Gardening",
        extra=(
            DiscoveredCandidate(
                "https://publisher.example/compost",
                title="Compost",
            ),
        ),
    )
    reversed_candidates = saved.model_copy(update={"candidates": tuple(reversed(saved.candidates))})
    assert report(source, planned, (saved,)) == report(source, planned, (reversed_candidates,))
    changed = manifest("[IMAGE: Different scene]")
    assert report(changed, planned, ()) == report(changed, tuple(reversed(planned)), ())


def test_authoritative_success_survives_wall_clock_step_backward(monkeypatch):
    from datetime import datetime

    from katcha.editorial.source_intelligence import jobs as ledger_module

    times = iter(
        (
            datetime.fromisoformat("2026-10-11T20:00:02+00:00"),
            datetime.fromisoformat("2026-10-11T20:00:01+00:00"),
        )
    )
    monkeypatch.setattr(ledger_module, "_now", lambda: next(times))
    source = manifest()
    planned = jobs(source)
    saved = page(planned[0])
    assert saved.receipt.finished_at < saved.receipt.started_at
    result = report(source, planned, (saved,))
    row = result.requirements[0]
    assert row.coverage_status == "reference_only"
    assert len(row.references) == 1
    warning = next(gap for gap in row.gaps if gap.category == "receipt_clock_order_warning")
    assert warning.receipt_refs == (saved.receipt.receipt_id,)
    assert LEDGER.read(planned[0].job_id, scope=source.scope) == saved.receipt
    assert row.usable_media_count == 0
