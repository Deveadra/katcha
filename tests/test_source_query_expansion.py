"""Offline query expansion, policy bounds and durable no-network replay proof."""

import hashlib
from dataclasses import replace
from uuid import UUID

import httpx
import pytest
from pydantic import ValidationError

from katcha.acquisition.adapters import DiscoveredCandidate, DiscoveryBatch
from katcha.editorial.project_schemas import EditorialScriptSeed
from katcha.editorial.source_intelligence.contracts import ProviderPolicyV1
from katcha.editorial.source_intelligence.jobs import SearchJobLedger
from katcha.editorial.source_intelligence.planner import SearchCapability, adapter_query
from katcha.editorial.source_intelligence.query_expansion import (
    ProviderRoute,
    QueryExpansionV1,
    QueryHintV1,
    expand_queries,
)
from katcha.editorial.source_intelligence.requirements import build_asset_manifest
from katcha.editorial.source_intelligence.scouting import ScoutRequest, scout


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def reject(*args, **kwargs):
        pytest.fail("real network prohibited")

    monkeypatch.setattr(httpx.Client, "request", reject)


def manifest(text="[VISUAL: White Vision at the window]\nNarration."):
    return build_asset_manifest(
        EditorialScriptSeed(
            text=text,
            origin="operator_paste",
            content_sha256=hashlib.sha256(text.encode()).hexdigest(),
        ),
        channel_id=UUID(int=1),
        project_id=UUID(int=2),
    )


def route(key="owned_fixture", **kwargs):
    return ProviderRoute(
        SearchCapability(key, "v1", "fixture", 100, external=False, supports_negative_terms=True),
        ProviderPolicyV1(version="fixture-v1", max_calls=1000, max_results=10000),
        "owned",
        **kwargs,
    )


def expansion(source, hints=None, routes=None, **kwargs):
    return expand_queries(
        source, routes=routes or (route(),), hints=hints, requested_by="offline-fixture", **kwargs
    )


def test_alias_publisher_alternatives_negative_terms_with_explicit_attribution():
    source = manifest()
    req = source.requirements[0]
    hints = {
        req.requirement_id: (
            QueryHintV1(kind="entity_alias", entity="White Vision", value="Spectral Vision"),
            QueryHintV1(
                kind="publisher",
                value="Marvel Studios",
                basis="operator_evidence",
                evidence_refs=("receipt-operator-1",),
            ),
            QueryHintV1(kind="alternative", value="diagram of Vision identities"),
            QueryHintV1(
                kind="negative",
                value="fan edit",
                basis="operator_evidence",
                evidence_refs=("policy-1",),
            ),
        )
    }
    result = expansion(source, hints)
    first = result.requirements[0]
    assert len(first.jobs) == len(first.variants) == 4
    assert first.supplied_hints == hints[req.requirement_id]
    assert [item.treatment for item in first.variants] == [
        "original",
        "entity_alias",
        "publisher",
        "alternative",
    ]
    assert first.variants[1].intent.query_terms == ("Spectral Vision at the window",)
    assert first.variants[1].basis == "hypothesis"
    assert first.variants[2].evidence_refs == ("receipt-operator-1",)
    assert first.variants[3].basis == "hypothesis"
    for job in first.jobs:
        assert job.semantic_brief == req.intent
        assert job.negative_terms == ("fan edit",)
        assert adapter_query(job, route().capability)["exclude_terms"] == ["fan edit"]
        job.verify_identity()
    assert result.requirements[1].gaps[0].category == "visual_purpose_unknown"
    assert not result.requirements[1].jobs
    assert source == manifest()
    assert QueryExpansionV1.model_validate_json(result.model_dump_json()) == result


def test_long_intent_preserved_in_brief_and_terms_without_truncation():
    source = manifest("[VISUAL: " + "evidence " * 50 + "]\nNarration.")
    req = source.requirements[0]
    result = expansion(source)
    job = result.requirements[0].jobs[0]
    assert job.semantic_brief == req.intent
    assert " ".join(job.query_terms) == req.intent
    assert all(len(term) <= 200 for term in job.query_terms)
    assert len(job.query_terms) > 1


def test_unsupported_negative_terms_are_visible_gap_never_silently_dropped():
    source = manifest()
    configured = route()
    configured = replace(
        configured, capability=replace(configured.capability, supports_negative_terms=False)
    )
    result = expansion(
        source,
        {source.requirements[0].requirement_id: (QueryHintV1(kind="negative", value="fan edit"),)},
        routes=(configured,),
    )
    first = result.requirements[0]
    assert not first.jobs
    assert first.variants[0].intent.negative_terms == ("fan edit",)
    assert first.gaps[0].category == "provider_cannot_preserve_query"
    assert "negative" in first.gaps[0].next_action


def test_source_policy_not_widened_external_search_not_implicitly_authorized():
    source = manifest()
    configured = route()
    wrong_class = replace(configured, source_class="official")
    result = expansion(source, routes=(wrong_class,))
    assert not result.jobs
    assert result.requirements[0].gaps[0].category == "source_class_not_approved"
    external = replace(configured, capability=replace(configured.capability, external=True))
    result = expansion(source, routes=(external,))
    assert not result.jobs
    assert result.requirements[0].gaps[0].category == "external_search_not_authorized"


def test_placeholder_uses_only_explicit_alternative_and_unrelated_alias_reports_gap():
    source = manifest("Narration without a visual.")
    req = source.requirements[0]
    result = expansion(
        source,
        {
            req.requirement_id: (
                QueryHintV1(
                    kind="entity_alias", entity="Unknown Character", value="Other Character"
                ),
                QueryHintV1(kind="alternative", value="commentary graphic"),
            )
        },
    )
    first = result.requirements[0]
    assert first.gaps[0].category == "alias_entity_not_in_intent"
    assert len(first.jobs) == 1
    assert first.jobs[0].query_terms == ("commentary graphic",)
    assert first.jobs[0].semantic_brief == req.intent
    assert first.variants[0].basis == "hypothesis"
    assert req.coverage_status == "needs_operator_input"


def test_120_requirements_two_providers_replay_do_not_reissue_known_requests(tmp_path):
    source = manifest("\n".join(f"[IMAGE: historical diagram {number}]" for number in range(120)))
    routes = (route("first"), route("second"))
    planned = expansion(source, routes=routes)
    assert len(planned.requirements) == 120
    assert len(planned.jobs) == 240
    assert planned == expansion(source, routes=routes)

    class FixtureAdapter:
        version = "v1"

        def __init__(self, key):
            self.key = key
            self.calls = 0

        def discover(self, query, cursor):
            self.calls += 1
            return DiscoveryBatch(
                items=(
                    DiscoveredCandidate(
                        "https://publisher.example/reference",
                        title="Fixture reference",
                    ),
                )
            )

    adapters = {
        item.capability.provider_key: FixtureAdapter(item.capability.provider_key)
        for item in routes
    }
    configured = {item.capability.provider_key: item for item in routes}
    requests = tuple(
        ScoutRequest(
            job,
            configured[job.provider_key].policy,
            adapters[job.provider_key],
            configured[job.provider_key].capability,
        )
        for job in planned.jobs
    )
    path = tmp_path / "queries.sqlite"
    pages = []
    replayed = []
    for offset in range(0, len(requests), 20):
        batch = requests[offset : offset + 20]
        pages.extend(scout(SearchJobLedger(path), batch, scope=source.scope))
        replayed.extend(scout(SearchJobLedger(path), batch, scope=source.scope))
    assert replayed == pages
    assert all(page.receipt.status == "succeeded" for page in pages)
    assert all(page.candidates[0].rights_state == "not_assessed" for page in pages)
    assert adapters["first"].calls == adapters["second"].calls == 120


def test_job_bound_keeps_every_requirement_and_reports_unscheduled_gaps():
    source = manifest("\n".join(f"[IMAGE: diagram {number}]" for number in range(120)))
    result = expansion(source, max_jobs=10)
    assert len(result.requirements) == 120
    assert len(result.jobs) == 10
    assert all(
        item.gaps[0].category == "planning_budget_exhausted" for item in result.requirements[10:]
    )


def test_duplicate_variants_and_routes_and_invalid_hint_bounds():
    source = manifest()
    req = source.requirements[0]
    hints = {req.requirement_id: (QueryHintV1(kind="alternative", value=req.intent),) * 2}
    # Original and duplicate alternative share the query but retain different purpose.
    assert len(expansion(source, hints).requirements[0].jobs) == 2
    with pytest.raises(ValueError, match="duplicate provider"):
        expansion(source, routes=(route(), route()))
    with pytest.raises(ValueError, match="unknown requirements"):
        expansion(source, {"missing": ()})
    with pytest.raises(ValueError, match="20 query hints"):
        expansion(source, {req.requirement_id: hints[req.requirement_id] * 11})
    with pytest.raises(ValidationError, match="receipt references"):
        QueryHintV1(kind="publisher", value="Studio", basis="operator_evidence")
    with pytest.raises(ValidationError, match="entity alias"):
        QueryHintV1(kind="entity_alias", value="Alias")
    with pytest.raises(ValueError, match="job bound"):
        expansion(source, max_jobs=True)


def test_paid_adapter_gap_and_provider_result_budget_are_explicit():
    source = manifest("[IMAGE: one]\n[IMAGE: two]\n[IMAGE: three]")
    configured = route()
    paid = replace(configured, capability=replace(configured.capability, potentially_paid=True))
    result = expansion(source, routes=(paid,))
    assert not result.jobs
    assert all(
        item.gaps[0].category == "paid_adapter_has_no_enforced_request_ceiling"
        for item in result.requirements
    )
    bounded = replace(configured, policy=ProviderPolicyV1(version="limited", max_results=20))
    result = expansion(source, routes=(bounded,))
    assert len(result.jobs) == 1
    assert all(
        item.gaps[0].category == "provider_plan_budget_exhausted"
        for item in result.requirements[1:]
    )
    with pytest.raises(ValueError, match="job bound"):
        expansion(source, max_jobs=1.5)


def test_maximum_intent_hypothesis_growth_reports_gap_instead_of_losing_text():
    # Requirement contract allows 4000-character operator meanings beyond small planner cues.
    source = manifest()
    raw = source.requirements[0]
    long_intent = "A " * 1999 + "A"
    # Make the preserved authoritative cue and its adapter intent agree.
    value = source.model_dump()
    value["plan"]["requirements"][0]["intent"] = long_intent
    value["requirements"][0]["intent"] = long_intent
    from katcha.editorial.source_intelligence.requirements import AssetManifestV1

    source = AssetManifestV1.model_validate(value)
    hints = {raw.requirement_id: (QueryHintV1(kind="entity_alias", entity="A", value="B" * 200),)}
    result = expansion(source, hints)
    first = result.requirements[0]
    assert len(first.jobs) == 1
    assert first.jobs[0].semantic_brief == long_intent
    assert " ".join(first.jobs[0].query_terms) == long_intent
    assert first.gaps[0].category == "query_bound_exceeded"


def test_bounded_followup_batches_skip_known_ids_and_eventually_cover_all_cues():
    source = manifest("\n".join(f"[IMAGE: diagram {number}]" for number in range(120)))
    known = frozenset()
    planned = []
    for _batch in range(12):
        result = expansion(source, max_jobs=10, known_job_ids=known)
        assert len(result.requirements) == 120
        assert len(result.jobs) == 10
        assert all(job.job_id not in known for job in result.jobs)
        refs = {key for item in result.requirements for key in item.known_job_ids}
        assert refs == known
        planned.extend(result.jobs)
        known = known | frozenset(job.job_id for job in result.jobs)
    assert len(known) == len(planned) == 120
    completed = expansion(source, max_jobs=10, known_job_ids=known)
    assert not completed.jobs
    assert not any(item.gaps for item in completed.requirements)
    assert {item.requirement_id for item in planned} == {
        item.requirement_id for item in source.requirements
    }


def test_alias_phrase_boundaries_case_and_literal_replacement():
    source = manifest("[VISUAL: Television shows VISION with Vision at a window]")
    req = source.requirements[0]
    result = expansion(
        source,
        {
            req.requirement_id: (
                QueryHintV1(kind="entity_alias", entity="vision", value=r"White Vision \1"),
            )
        },
    )
    assert result.requirements[0].variants[1].intent.query_terms == (
        r"Television shows White Vision \1 with White Vision \1 at a window",
    )
    assert not result.requirements[0].gaps
    with pytest.raises(ValueError, match="valid search job IDs"):
        expansion(source, known_job_ids=frozenset({"invalid"}))


def test_known_later_variant_references_survive_exhausted_new_job_bound():
    source = manifest("[IMAGE: One]\n[IMAGE: Two]")
    req = source.requirements[1]
    hints = {req.requirement_id: (QueryHintV1(kind="alternative", value="second alternative"),)}
    all_jobs = expansion(source, hints).jobs
    known = frozenset({all_jobs[-1].job_id})
    limited = expansion(source, hints, max_jobs=1, known_job_ids=known)
    assert len(limited.jobs) == 1
    assert limited.requirements[1].known_job_ids == tuple(known)
    assert limited.requirements[1].gaps[0].category == "planning_budget_exhausted"


def test_serialized_expansion_rejects_cross_scope_and_duplicate_jobs():
    result = expansion(manifest())
    value = result.model_dump()
    value["requirements"][0]["jobs"][0]["scope"]["channel_id"] = UUID(int=999)
    with pytest.raises(ValidationError, match="crosses scope"):
        QueryExpansionV1.model_validate(value)
    value = result.model_dump()
    value["requirements"][0]["jobs"] *= 2
    with pytest.raises(ValidationError, match="duplicate or unbounded"):
        QueryExpansionV1.model_validate(value)
