"""KSI-106: real script manifests + canonical Bin + generated measured footage.

Fixtures demonstrate local evidence handling, never semantic recognition or live rights.
"""

from __future__ import annotations

import hashlib
import uuid

import pytest
from sqlalchemy import select
from test_shot_index_search import (  # noqa: F401
    build,
    indexed_context,
    known_video,
    licensed_evidence,
)

from katcha import db
from katcha.editorial.media_preparation_schemas import StartMediaPreparation
from katcha.editorial.project_schemas import EditorialScriptSeed, SaveEditorialDraft
from katcha.editorial.source_intelligence.contracts import ProviderPolicyV1
from katcha.editorial.source_intelligence.jobs import SearchJobLedger
from katcha.editorial.source_intelligence.planner import SearchCapability
from katcha.editorial.source_intelligence.query_expansion import ProviderRoute
from katcha.editorial.source_intelligence.shot_contracts import (
    MatchRequirementsRequest,
    SelectShotRequest,
)
from katcha.editorial_models import EditorialProject, ProjectMediaItem
from katcha.models import Clip, SourceItem
from katcha.services import editorial_media_preparation as preparation
from katcha.services import editorial_shot_matching as matching
from katcha.services.editorial_media_bin import add_project_media_item, get_project_media_item
from katcha.services.editorial_projects import save_draft
from katcha.shot_matching_models import OperatorShotChoice, RequirementShotPackage


@pytest.fixture
def match_context(
    indexed_context,  # noqa: F811
    monkeypatch,
    tmp_path,
):
    monkeypatch.setattr(
        preparation,
        "runtime",
        lambda: preparation.PreparationRuntime(
            ledger=SearchJobLedger(tmp_path / "ledger.sqlite"),
            routes=(
                ProviderRoute(
                    SearchCapability("fixture", "v1", "fixture", 10),
                    ProviderPolicyV1(version="fixture-v1"),
                    "public_reference",
                    10,
                ),
            ),
            adapters={},
        ),
    )
    build(indexed_context)
    return indexed_context


def prepare(context, text="[VISUAL: red]", key="prepare"):
    channel, _, project, _, _, _, _ = context
    seed = EditorialScriptSeed(
        text=text, origin="operator_paste", content_sha256=hashlib.sha256(text.encode()).hexdigest()
    )
    with db.session_scope() as session:
        row = session.get(EditorialProject, project)
        row.brief = {**row.brief, "script_seed": seed.model_dump(mode="json")}
        revision = row.revision
    return preparation.start_preparation(
        channel,
        project,
        StartMediaPreparation(
            expected_revision=revision, idempotency_key=key, external_search_authorized=False
        ),
        actor="fixture",
    )


def request(context, run, key="match", items=None):
    return MatchRequirementsRequest(
        preparation_run_id=run.id,
        expected_revision=run.input_revision,
        item_ids=items or (context[3],),
        idempotency_key=key,
    )


def match(context, body):
    return matching.match_requirements(
        context[0], context[2], body, actor="fixture", store=context[-1]
    )


def choose(context, package, key="choice", version=0, note="Keep my selected shot"):
    binding = next(value for result in package["requirements"] for value in result["proposals"])
    body = SelectShotRequest(
        package_id=package["package_id"],
        binding_id=binding["binding_id"],
        expected_revision=package["input_revision"],
        expected_choice_version=version,
        idempotency_key=key,
        note=note,
    )
    return matching.select_shot(context[0], context[2], body, actor="operator", store=context[-1])


def test_measured_source_relative_rank_explanation_identity_and_replay(match_context, monkeypatch):
    context = match_context
    run = prepare(context)
    body = request(context, run)
    from katcha.editorial.media_intake import scene_index

    monkeypatch.setattr(scene_index, "_measure", lambda *args: pytest.fail("replay decoded media"))
    first = match(context, body)
    second = match(context, body)
    assert first == second and first["current_status"] == "current"
    hits = first["requirements"][0]["proposals"]
    assert len(hits) == 1
    shot = hits[0]
    assert shot["source_origin_tick"] > 0
    assert shot["source_in_seconds"] == pytest.approx(0, abs=0.1)
    assert shot["source_out_seconds"] == pytest.approx(1, abs=0.1)
    assert shot["start_tick"] <= shot["keyframe_tick"] < shot["end_tick"] <= shot["duration_ticks"]
    assert shot["rank_score"] == 2 and shot["match_basis"] == "measured_visual_tokens"
    assert shot["evidence_refs"] and shot["confidence_limits"] and shot["explanation"]
    assert not shot["production_eligible"] and not shot["rights_granted"]
    assert first["provider_calls"] == 0
    with db.session_scope() as session:
        assert len(list(session.scalars(select(RequirementShotPackage)))) == 1
    changed = body.model_copy(update={"item_ids": (uuid.uuid4(),)})
    with pytest.raises(ValueError, match="not found"):
        match(context, changed)


def test_full_script_more_than_one_hundred_requirements_preserved(match_context):
    context = match_context
    text = "\n".join(f"Narration {number}.\n[VISUAL: red]" for number in range(101))
    run = prepare(context, text)
    result = match(context, request(context, run))
    expected = run.artifacts["manifest"]["requirements"]
    assert len(expected) > 101
    assert len(result["requirements"]) == len(expected)
    assert {value["requirement_id"] for value in result["requirements"]} == {
        value["requirement_id"] for value in expected
    }
    assert all(value["proposals"] or value["unresolved_gaps"] for value in result["requirements"])
    with db.session_scope() as session:
        assert session.get(EditorialProject, context[2]).brief["script_seed"]["text"] == text


@pytest.mark.parametrize(
    "intent", ["Vision opens a red door", "bright car chase", "a character", "red car"]
)
def test_color_never_asserts_characters_actions_or_scenes(match_context, intent):
    context = match_context
    run = prepare(context, f"[VISUAL: {intent}]")
    result = match(context, request(context, run))
    assert not result["requirements"][0]["proposals"]
    assert "semantic_intent_unobserved" in result["requirements"][0]["unresolved_gaps"]
    with db.session_scope() as session:
        item = session.get(ProjectMediaItem, context[3])
        item.requirement_ids = [run.artifacts["manifest"]["requirements"][0]["requirement_id"]]
    fallback = match(context, request(context, run, "linked"))
    hits = fallback["requirements"][0]["proposals"]
    assert hits and all(value["match_basis"] == "requirement_link_fallback" for value in hits)
    assert all(value["match_confidence"] == "unverified_semantics" for value in hits)
    assert "semantic_intent_unobserved" in fallback["requirements"][0]["unresolved_gaps"]


@pytest.mark.parametrize("wrong", ["channel", "project", "preparation", "item"])
def test_scope_checked_before_storage(match_context, wrong):
    context = match_context
    run = prepare(context)
    body = request(context, run)
    channel, project = context[0], context[2]
    if wrong == "channel":
        channel = context[1]
    elif wrong == "project":
        project = uuid.uuid4()
    elif wrong == "preparation":
        body = body.model_copy(update={"preparation_run_id": uuid.uuid4()})
    else:
        body = body.model_copy(update={"item_ids": (context[3], uuid.uuid4())})
    before = context[-1].calls
    with pytest.raises(ValueError, match="not found"):
        matching.match_requirements(channel, project, body, actor="fixture", store=context[-1])
    assert context[-1].calls == before


def test_operator_locked_choices_notes_and_bin_survive_recomputation(match_context):
    context = match_context
    run = prepare(context)
    original_item = get_project_media_item(context[0], context[2], context[3])
    first = match(context, request(context, run))
    choice = choose(context, first)
    assert choose(context, first) == choice  # exact lost-response replay
    second = match(context, request(context, run, "recompute"))
    assert second["operator_choices"][0]["locked"]
    assert second["operator_choices"][0]["note"] == choice["note"]
    assert second["operator_choices"][0]["binding"]["binding_id"] == choice["binding"]["binding_id"]
    with pytest.raises(ValueError, match="choice changed"):
        choose(context, second, "replace")
    replaced = choose(context, second, "replace", version=1, note="Explicit replacement")
    assert replaced["version"] == 2
    with db.session_scope() as session:
        assert len(list(session.scalars(select(OperatorShotChoice)))) == 2
    item = get_project_media_item(context[0], context[2], context[3])
    assert item.notes == original_item.notes and item.provenance == original_item.provenance
    assert (
        item.requirement_ids == original_item.requirement_ids
        and item.status == original_item.status
    )


def test_revision_change_keeps_choice_but_invalidates_proposals_and_replay(match_context):
    context = match_context
    run = prepare(context)
    package = match(context, request(context, run))
    choose(context, package)
    save_draft(
        context[0],
        context[2],
        SaveEditorialDraft(
            expected_revision=run.input_revision,
            idempotency_key="changed-script",
            draft={"script": []},
        ),
        actor="operator",
    )
    saved = matching.get_shot_package(
        context[0], context[2], uuid.UUID(package["package_id"]), store=context[-1]
    )
    assert saved["current_status"] == "stale_revision"
    assert saved["operator_choices"][0]["note"] == "Keep my selected shot"
    assert not saved["operator_choices"][0]["binding"]["production_eligible"]
    with pytest.raises(ValueError, match="changed"):
        match(context, request(context, run))
    with pytest.raises(ValueError, match="stale"):
        choose(context, package, "new-choice", version=1)


@pytest.mark.parametrize("revocation", ["bin", "upload", "latest_assessment"])
def test_revocation_preserves_choice_and_blocks_current_eligibility(match_context, revocation):
    context = match_context
    if revocation == "latest_assessment":
        candidate, _ = licensed_evidence(context)
    run = prepare(context)
    package = match(context, request(context, run))
    selected = choose(context, package)
    if revocation == "latest_assessment":
        assert selected["binding"]["production_eligible"]
    before = context[-1].calls
    with db.session_scope() as session:
        if revocation == "bin":
            session.get(ProjectMediaItem, context[3]).rights_status = "revoked"
        elif revocation == "upload":
            session.get(SourceItem, context[5]).source_metadata = {
                "channel_profile_id": str(context[0])
            }
        else:
            from katcha.acquisition_models import RightsAssessment

            session.add(
                RightsAssessment(
                    id=uuid.uuid4(),
                    discovery_candidate_id=candidate,
                    version=2,
                    production_eligible=False,
                )
            )
    result = matching.get_shot_package(
        context[0], context[2], uuid.UUID(package["package_id"]), store=context[-1]
    )
    assert result["current_status"] == "stale_source_or_permission"
    assert context[-1].calls == before
    assert not result["operator_choices"][0]["binding"]["production_eligible"]
    assert result["operator_choices"][0]["binding"]["current_status"] != "current"
    assert result["operator_choices"][0]["note"] == selected["note"]
    with pytest.raises(ValueError, match="stale"):
        choose(context, package, "replacement", version=1)


def test_actual_byte_corruption_with_unchanged_db_sha_blocks_read_and_replay(match_context):
    context = match_context
    run = prepare(context)
    body = request(context, run)
    package = match(context, body)
    context[-1].data += b"changed storage bytes"
    with pytest.raises(ValueError, match="bytes changed"):
        matching.get_shot_package(
            context[0], context[2], uuid.UUID(package["package_id"]), store=context[-1]
        )
    with pytest.raises(ValueError, match="bytes changed"):
        match(context, body)


def test_permission_change_during_verification_cannot_publish(match_context):
    context = match_context
    run = prepare(context)

    def revoke():
        with db.session_scope() as session:
            session.get(ProjectMediaItem, context[3]).rights_status = "revoked"

    context[-1].after_read = revoke
    with pytest.raises(ValueError, match="permission"):
        match(context, request(context, run))
    with db.session_scope() as session:
        assert not list(session.scalars(select(RequirementShotPackage)))


def test_revision_change_during_verification_cannot_publish(match_context):
    context = match_context
    run = prepare(context)

    def change():
        context[-1].after_read = None
        save_draft(
            context[0],
            context[2],
            SaveEditorialDraft(
                expected_revision=run.input_revision,
                idempotency_key="concurrent-edit",
                draft={"script": []},
            ),
            actor="operator",
        )

    context[-1].after_read = change
    with pytest.raises(ValueError, match="changed"):
        match(context, request(context, run))
    with db.session_scope() as session:
        assert not list(session.scalars(select(RequirementShotPackage)))


def test_bounds_and_budget_gaps_never_drop_requirements(match_context, monkeypatch):
    context = match_context
    run = prepare(context, "[VISUAL: red]\n[VISUAL: blue]\n[VISUAL: green]")
    monkeypatch.setattr(matching, "MAX_PROPOSALS", 1)
    result = match(context, request(context, run))
    assert len(result["requirements"]) == 3
    assert sum(len(value["proposals"]) for value in result["requirements"]) == 1
    assert any(
        any("budget" in gap for gap in value["unresolved_gaps"]) for value in result["requirements"]
    )
    with db.session_scope() as session:
        session.get(ProjectMediaItem, context[3]).source_out_seconds = 0.5
    bounded = match(context, request(context, run, "bounds"))
    assert all(not value["proposals"] for value in bounded["requirements"])


def test_same_request_key_changed_input_and_current_hash_fail_closed(match_context):
    context = match_context
    run = prepare(context)
    body = request(context, run)
    match(context, body)
    with db.session_scope() as session:
        session.get(ProjectMediaItem, context[3]).notes = "Changed operator context"
    with pytest.raises(ValueError, match="changed inputs"):
        match(context, body)
    with db.session_scope() as session:
        session.get(Clip, context[4]).sha256 = "f" * 64
    result = match(context, request(context, run, "new-content"))
    assert not result["requirements"][0]["proposals"]
    assert result["item_gaps"][str(context[3])] == "current_measured_index_missing"


def test_shared_clip_items_verify_once_per_snapshot_and_rank_stably(match_context):
    context = match_context
    run = prepare(context)
    second = add_project_media_item(
        context[0],
        context[2],
        run.input_revision,
        request_key="second-reference",
        actor="operator",
        kind="video",
        clip_id=context[4],
    )
    body = request(context, run, items=(second.id, context[3]))
    result = match(context, body)
    hits = result["requirements"][0]["proposals"]
    assert len(hits) == 2
    assert [value["media_item_id"] for value in hits] == sorted(
        value["media_item_id"] for value in hits
    )
    assert hits[0]["index_id"] == hits[1]["index_id"]
    assert hits[0]["binding_id"] != hits[1]["binding_id"]


def test_api_authorization_payload_bounds_and_no_external_timestamps(match_context, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from katcha.api import editorial_shot_matching as routes

    context = match_context
    run = prepare(context)
    app = FastAPI()
    calls = []

    @app.middleware("http")
    async def authorize(request, call_next):
        request.state.control_actor = "fixture"
        request.state.control_principal_name = "fixture"
        request.state.control_scopes = {"ai:read"}
        if request.headers.get("x-writer"):
            request.state.control_scopes.add("production:create")
        request.state.control_channel_profile_ids = {str(context[0])}
        return await call_next(request)

    monkeypatch.setattr(
        routes, "match_requirements", lambda *args, **kwargs: calls.append(args) or {}
    )
    monkeypatch.setattr(
        routes, "get_shot_package", lambda *args, **kwargs: calls.append(args) or {}
    )
    monkeypatch.setattr(routes, "select_shot", lambda *args, **kwargs: calls.append(args) or {})
    app.include_router(routes.router)
    base = f"/v1/channels/{context[0]}/editorial-projects/{context[2]}/shot-proposals"
    body = request(context, run).model_dump(mode="json")
    with TestClient(app) as client:
        assert client.post(base, json=body).status_code == 403
        assert client.post(base, json=body, headers={"x-writer": "1"}).status_code == 200
        assert (
            client.post(
                base, json={**body, "source_in_seconds": 123}, headers={"x-writer": "1"}
            ).status_code
            == 422
        )
        assert (
            client.post(
                base, json={**body, "item_ids": [str(context[3])] * 9}, headers={"x-writer": "1"}
            ).status_code
            == 422
        )
        assert client.get(f"{base}/{uuid.uuid4()}").status_code == 200
        before = len(calls)
        assert (
            client.get(
                f"{base}/{uuid.uuid4()}".replace(str(context[0]), str(context[1]))
            ).status_code
            == 403
        )
        assert len(calls) == before


@pytest.mark.parametrize(
    "field,value",
    [
        ("end_tick", 99999999),
        ("start_tick", 99999999),
        ("keyframe_tick", 99999999),
        ("source_in_seconds", 50),
        ("source_out_seconds", 100),
        ("source_out_seconds", float("nan")),
    ],
)
def test_binding_contract_rejects_unmeasured_or_out_of_bounds_intervals(
    match_context, field, value
):
    from katcha.editorial.source_intelligence.shot_contracts import RequirementBindingV1

    run = prepare(match_context)
    package = match(match_context, request(match_context, run))
    binding = package["requirements"][0]["proposals"][0]
    with pytest.raises(ValueError):
        RequirementBindingV1.model_validate({**binding, field: value})
    assert RequirementBindingV1.model_validate(binding).end_tick <= binding["duration_ticks"]


def test_reference_only_selection_reports_gaps_without_media_access(match_context):
    context = match_context
    run = prepare(context)
    item = add_project_media_item(
        context[0],
        context[2],
        run.input_revision,
        request_key="reference",
        actor="operator",
        kind="video",
        external_reference_url="https://publisher.example/reference",
    )
    before = context[-1].calls
    result = match(context, request(context, run, items=(item.id,)))
    assert context[-1].calls == before and not result["requirements"][0]["proposals"]
    assert str(item.id) in result["item_gaps"]


def test_current_production_choice_requires_revalidation_from_other_package(match_context):
    context = match_context
    licensed_evidence(context)
    run = prepare(context)
    first = match(context, request(context, run))
    choose(context, first)
    second = match(context, request(context, run, "second"))
    choice = second["operator_choices"][0]
    assert choice["current_status"] == "requires_revalidation"
    assert not choice["binding"]["production_eligible"]
    assert choice["binding"]["current_status"] == "requires_revalidation"


def test_changed_source_during_ranking_fences_publication(match_context, monkeypatch):
    context = match_context
    run = prepare(context)
    original = matching._rank

    def revoke_after_ranking(*args, **kwargs):
        result = original(*args, **kwargs)
        with db.session_scope() as session:
            session.get(ProjectMediaItem, context[3]).rights_status = "revoked"
        return result

    monkeypatch.setattr(matching, "_rank", revoke_after_ranking)
    with pytest.raises(ValueError, match="changed before saving"):
        match(context, request(context, run))
    with db.session_scope() as session:
        assert not list(session.scalars(select(RequirementShotPackage)))
