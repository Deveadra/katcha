"""Generated footage + durable real gateway receipts; no live provider transmissions."""
# ruff: noqa: F811

from __future__ import annotations

import json
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from test_requirement_shot_matching import (  # noqa: F401
    match_context,
    prepare,
)
from test_shot_index_search import indexed_context, known_video  # noqa: F401

from katcha import db
from katcha.editorial import provider
from katcha.editorial.media_intake.scene_schemas import SceneIndexV1
from katcha.editorial.media_intake.semantic_frames import contact_sheet
from katcha.editorial.semantic_schemas import SemanticMatchRequest
from katcha.editorial_models import EditorialProject, EditorialRun, ProjectMediaItem
from katcha.services import editorial_semantic_matching as semantic
from katcha.services.editorial_projects import EditorialConflict
from katcha.services.editorial_runs import control_run


def request(context, run, **changes):
    body = SemanticMatchRequest(
        preparation_run_id=run.id,
        expected_revision=run.input_revision,
        media_item_id=context[3],
        requirement_ids=(run.artifacts["manifest"]["requirements"][0]["requirement_id"],),
        shot_ids=("shot-0000", "shot-0001", "shot-0002"),
        idempotency_key="semantic",
        external_inspection_authorized=True,
    )
    return body.model_copy(update=changes)


def match(context, body):
    return semantic.match_semantics(
        context[0], context[2], body, actor="fixture", store=context[-1]
    )


@pytest.fixture
def fixture_provider(monkeypatch):
    calls = []
    monkeypatch.setattr(provider, "select_provider", lambda **kwargs: "gemini")

    def invoke(selected_provider, prompt, schema, **kwargs):
        assert selected_provider == "gemini"
        assert not kwargs["search"] and kwargs["video"] is None
        assert kwargs["image"][:2] == b"\xff\xd8"
        assert kwargs["max_output_tokens"] == 2048
        context = json.loads(prompt.split("\n", 1)[1])
        calls.append(context)
        value = {
            "hypotheses": [
                {
                    "requirement_id": context["requirements"][0]["requirement_id"],
                    "shot_id": "shot-0000",
                    "visible_details_hypothesis": "Possibly a red object",
                    "match_explanation": "Fixture hypothesis only; inspect the original frame",
                    "confidence": "low",
                    "uncertainties": ["Identity and motion are unseen"],
                }
            ],
            "unresolved_requirement_ids": [],
        }
        return provider.ProviderOutput(json.dumps(value), "gemini", "fixture-only", 100, 50, [])

    monkeypatch.setattr(provider, "_invoke", invoke)
    return calls


def test_real_frame_bytes_durable_hypotheses_never_promote_choices(match_context, fixture_provider):
    context = match_context
    run = prepare(context, "[VISUAL: red vehicle]")
    with db.session_scope() as session:
        session.get(ProjectMediaItem, context[3]).notes = "Preserve operator note"
    body = request(context, run)
    first = match(context, body)
    assert first["status"] == "completed" and len(fixture_provider) == 1
    result = first["result"]
    assert result["schema_version"] == "SemanticShotSuggestionsV1"
    hypothesis = result["hypotheses"][0]
    assert not hypothesis["verified_fact"] and not hypothesis["production_eligible"]
    assert not hypothesis["rights_granted"] and hypothesis["human_review_required"]
    assert hypothesis["measured_frame"]["source_in_seconds"] == 0
    assert hypothesis["measured_frame"]["source_out_seconds"] == 1
    assert len(hypothesis["evidence_refs"]) >= 5 and hypothesis["confidence_limits"]
    assert result["unresolved_gaps"][body.requirement_ids[0]]
    receipt = first["provider_calls"][semantic.CALL_KEY]
    assert receipt["image_sha256"] and receipt["output_token_limit"] == 2048
    assert receipt["coverage"] == "sampled_frames" and receipt["submissions"] == 1
    assert match(context, body) == first and len(fixture_provider) == 1
    with db.session_scope() as session:
        item = session.get(ProjectMediaItem, context[3])
        assert item.notes == "Preserve operator note" and item.rights_status == "not_assessed"
        assert item.source_in_seconds is None and item.source_out_seconds is None
        assert (
            session.get(EditorialProject, context[2]).brief["script_seed"]["text"]
            == "[VISUAL: red vehicle]"
        )


def test_default_off_before_source_access_or_provider(match_context, fixture_provider):
    run = prepare(match_context)
    before = match_context[-1].calls
    with pytest.raises(EditorialConflict, match="off"):
        match(match_context, request(match_context, run, external_inspection_authorized=False))
    assert not fixture_provider and match_context[-1].calls == before


@pytest.mark.parametrize("wrong", ["channel", "project", "item", "requirement", "shot"])
def test_scope_and_unmeasured_ids_fail_before_provider(match_context, fixture_provider, wrong):
    context = match_context
    run = prepare(context)
    body = request(context, run)
    channel, project = context[0], context[2]
    if wrong == "channel":
        channel = context[1]
    elif wrong == "project":
        project = uuid.uuid4()
    elif wrong == "item":
        body = body.model_copy(update={"media_item_id": uuid.uuid4()})
    elif wrong == "requirement":
        body = body.model_copy(update={"requirement_ids": ("invented",)})
    else:
        body = body.model_copy(update={"shot_ids": ("invented",)})
    with pytest.raises(ValueError):
        semantic.match_semantics(channel, project, body, actor="fixture", store=context[-1])
    assert not fixture_provider


def test_measured_frame_hash_mismatch_blocks_model_pixels(match_context, known_video):
    context = match_context
    run = prepare(context)
    row, _ = semantic._context(context[0], context[2], request(context, run), context[-1])
    evidence = SceneIndexV1.model_validate(row["index"]["evidence"])
    evidence.shots[0].keyframe_sha256 = "0" * 64
    with pytest.raises(ValueError, match="hash changed"):
        contact_sheet(known_video, evidence, ["shot-0000"])


@pytest.mark.parametrize("mutation", ["rights", "bytes", "revision"])
def test_after_transmission_changes_cannot_publish(
    match_context, fixture_provider, monkeypatch, mutation
):
    context = match_context
    run = prepare(context)
    body = request(context, run)
    original = provider._invoke

    def invoke(*args, **kwargs):
        output = original(*args, **kwargs)
        if mutation == "bytes":
            context[-1].data += b"corrupt"
        else:
            with db.session_scope() as session:
                if mutation == "rights":
                    session.get(ProjectMediaItem, context[3]).rights_status = "revoked"
                else:
                    session.get(EditorialProject, context[2]).revision += 1
        return output

    monkeypatch.setattr(provider, "_invoke", invoke)
    with pytest.raises(EditorialConflict):
        match(context, body)
    with db.session_scope() as session:
        row = session.get(EditorialRun, uuid.uuid5(context[2], "semantic-matching:semantic"))
        assert row.status == "blocked" and "semantic_result" not in row.artifacts
        assert row.artifacts["provider_calls"][semantic.CALL_KEY]["status"] == "completed"
    assert len(fixture_provider) == 1


def test_timeout_restart_same_and_new_key_no_repeat(match_context, fixture_provider, monkeypatch):
    context = match_context
    run = prepare(context)
    body = request(context, run)
    count = []

    def timeout(*args, **kwargs):
        count.append(1)
        raise TimeoutError("fixture ambiguous provider outcome")

    monkeypatch.setattr(provider, "_invoke", timeout)
    with pytest.raises(TimeoutError):
        match(context, body)
    restarted = match(context, body)
    assert restarted["status"] == "blocked" and restarted["result"] is None
    assert restarted["provider_calls"][semantic.CALL_KEY]["status"] == "started"
    assert not restarted["automatic_retry_allowed"]
    with pytest.raises(EditorialConflict, match="already have a semantic receipt"):
        match(context, body.model_copy(update={"idempotency_key": "try-new-key"}))
    with pytest.raises(EditorialConflict, match="already have a semantic receipt"):
        match(
            context,
            body.model_copy(
                update={
                    "idempotency_key": "try-reorder",
                    "shot_ids": tuple(reversed(body.shot_ids)),
                }
            ),
        )
    with pytest.raises(EditorialConflict, match="cannot be resumed"):
        control_run(
            context[0], context[2], uuid.UUID(restarted["run_id"]), expected_attempt=1, cancel=False
        )
    assert len(count) == 1


def test_concurrent_workers_submit_once(match_context, fixture_provider, monkeypatch):
    context = match_context
    run = prepare(context)
    body = request(context, run)
    entered, release = threading.Event(), threading.Event()
    original = provider._invoke

    def invoke(*args, **kwargs):
        entered.set()
        assert release.wait(10)
        return original(*args, **kwargs)

    monkeypatch.setattr(provider, "_invoke", invoke)
    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(match, context, body)
        assert entered.wait(10)
        second = executor.submit(match, context, body)
        try:
            pending = second.result(timeout=10)
            assert pending["status"] == "running" and pending["result"] is None
        finally:
            release.set()
        assert first.result(timeout=10)["status"] == "completed"
    assert len(fixture_provider) == 1


@pytest.mark.parametrize("bad", ["shot", "timestamp", "high_confidence"])
def test_model_inventions_and_unbounded_confidence_are_not_promoted(
    match_context, fixture_provider, monkeypatch, bad
):
    context = match_context
    run = prepare(context)
    original = provider._invoke

    def invoke(*args, **kwargs):
        output = original(*args, **kwargs)
        value = json.loads(output.text)
        if bad == "shot":
            value["hypotheses"][0]["shot_id"] = "invented"
        elif bad == "timestamp":
            value["hypotheses"][0]["start_seconds"] = 99
        else:
            value["hypotheses"][0]["confidence"] = "high"
        output.text = json.dumps(value)
        return output

    monkeypatch.setattr(provider, "_invoke", invoke)
    with pytest.raises(ValueError):
        match(context, request(context, run))
    saved = match(context, request(context, run))
    assert saved["status"] == "blocked" and saved["result"] is None
    assert len(fixture_provider) == 1


def test_unsupported_subscription_provider_has_no_submission(
    match_context, fixture_provider, monkeypatch
):
    context = match_context
    run = prepare(context)
    monkeypatch.setattr(provider, "select_provider", lambda **kwargs: "codex")
    with pytest.raises(provider.EditorialBlocked, match="hard output-token cap"):
        match(context, request(context, run))
    saved = match(context, request(context, run))
    assert saved["provider_calls"] == {} and not fixture_provider


@pytest.mark.asyncio
async def test_dispatch_does_not_replay_interrupted_semantic_run():
    from katcha.orchestration.editorial_dispatch import dispatch_editorial_run

    class NoDispatch:
        async def start_workflow(self, *args, **kwargs):
            pytest.fail("semantic run automatically dispatched")

    row = SimpleNamespace(
        id=uuid.uuid4(), attempt=1, status="running", options={"target": "semantic_matching"}
    )
    await dispatch_editorial_run(NoDispatch(), row)


def test_api_authorization_precedes_semantic_backend(monkeypatch):
    from katcha.api import editorial_semantic_matching as api

    def deny(*args, **kwargs):
        raise HTTPException(403, "fixture denied")

    monkeypatch.setattr(api, "_authorize", deny)
    monkeypatch.setattr(api, "match_semantics", lambda *args, **kwargs: pytest.fail("auth bypass"))
    with pytest.raises(HTTPException) as denied:
        api.propose(uuid.uuid4(), uuid.uuid4(), None, None)
    assert denied.value.status_code == 403


def test_explicit_new_identity_after_proven_pre_submission_failure(
    match_context, fixture_provider, monkeypatch
):
    context = match_context
    run = prepare(context)
    body = request(context, run)
    monkeypatch.setattr(provider, "select_provider", lambda **kwargs: "codex")
    with pytest.raises(provider.EditorialBlocked):
        match(context, body)
    monkeypatch.setattr(provider, "select_provider", lambda **kwargs: "gemini")
    assert match(context, body)["status"] == "blocked"
    assert (
        match(context, body.model_copy(update={"idempotency_key": "explicit-new-work"}))["status"]
        == "completed"
    )
    assert len(fixture_provider) == 1
    with pytest.raises(EditorialConflict, match="already have a semantic receipt"):
        match(context, body.model_copy(update={"idempotency_key": "do-not-repeat-after-local-fix"}))


def test_actual_gemini_sdk_receives_hard_output_cap_and_pixels(monkeypatch):
    from google import genai

    captured = {}

    def generate_content(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(text='{"hypotheses":[]}', candidates=[], usage_metadata=None)

    client = SimpleNamespace(
        models=SimpleNamespace(generate_content=generate_content), close=lambda: None
    )
    monkeypatch.setattr(genai, "Client", lambda **kwargs: client)
    monkeypatch.setattr(provider, "assert_mutation_authority", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        provider,
        "get_settings",
        lambda: SimpleNamespace(
            gemini_api_key="fixture-only", editorial_gemini_model="fixture-only"
        ),
    )
    from katcha.editorial.semantic_schemas import SemanticModelResult

    result = provider._invoke(
        "gemini",
        "fixture prompt",
        SemanticModelResult,
        search=False,
        image=b"fixture-pixels",
        video=None,
        max_output_tokens=2048,
    )
    assert captured["config"].max_output_tokens == 2048
    assert captured["contents"][1].inline_data.data == b"fixture-pixels"
    assert not captured["config"].tools and result.text == '{"hypotheses":[]}'
