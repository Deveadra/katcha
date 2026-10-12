"""Fixture-only approved shot handoff; no live provider, acquisition or render spend."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select
from test_requirement_shot_matching import choose, match
from test_requirement_shot_matching import request as match_request
from test_shot_index_search import indexed_context, known_video, licensed_evidence  # noqa: F401

from katcha import db
from katcha.acquisition_models import RightsAssessment
from katcha.editorial.media_intake import scene_index, scene_worker
from katcha.editorial.media_preparation_schemas import StartMediaPreparation
from katcha.editorial.project_schemas import EditorialDraft, SaveEditorialDraft
from katcha.editorial.run_schemas import StartEditorialRun
from katcha.editorial.shot_handoff_schemas import ApplyApprovedShot
from katcha.editorial.storyboard_schemas import SaveStoryboardWorkspace, StoryboardWorkspace
from katcha.editorial.visual_compiler import compile_project_visuals
from katcha.editorial.visual_schemas import StoryboardPlan
from katcha.editorial_models import EditorialRun, EditorialStoryboardRevision, ProjectMediaItem
from katcha.models import Clip
from katcha.services import editorial_media_preparation as preparation
from katcha.services.editorial_projects import save_draft
from katcha.services.editorial_runs import start_run
from katcha.services.editorial_shot_handoff import apply_approved_shot, choice_media_id
from katcha.services.editorial_storyboards import get_latest_storyboard, save_storyboard
from katcha.shot_matching_models import OperatorShotChoice


@pytest.fixture
def handoff_context(indexed_context, monkeypatch, tmp_path, request):  # noqa: F811
    monkeypatch.setattr(
        scene_index, "_measure", lambda path, sha, directory: scene_worker.measure_video(path, sha)
    )
    context = indexed_context
    licensed_evidence(context)
    monkeypatch.setattr(scene_index, "ObjectStore", lambda: context[-1])
    from katcha.editorial.source_intelligence.contracts import ProviderPolicyV1
    from katcha.editorial.source_intelligence.jobs import SearchJobLedger
    from katcha.editorial.source_intelligence.planner import SearchCapability
    from katcha.editorial.source_intelligence.query_expansion import ProviderRoute

    monkeypatch.setattr(
        preparation,
        "runtime",
        lambda: preparation.PreparationRuntime(
            ledger=SearchJobLedger(tmp_path / "jobs.sqlite"),
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
    with db.session_scope() as session:
        clip = session.get(Clip, context[4])
        clip.width, clip.height, clip.duration_seconds = 64, 36, 3
    save_draft(
        context[0],
        context[2],
        SaveEditorialDraft(
            expected_revision=1,
            idempotency_key="handoff-script",
            draft=EditorialDraft.model_validate(
                {
                    "script": [
                        {
                            "id": "beat-one",
                            "role": "hook",
                            "narration": "Look.",
                            "nonfactual": True,
                            "visual_intent": "red",
                            "planned_duration_seconds": 0.5,
                        }
                    ]
                    + (
                        [
                            {
                                "id": "beat-two",
                                "role": "closing",
                                "narration": "Hold.",
                                "nonfactual": True,
                                "visual_intent": "red",
                                "planned_duration_seconds": 0.5,
                            }
                        ]
                        if getattr(request, "param", False)
                        else []
                    )
                }
            ),
        ),
        actor="fixture",
    )
    scene_index.index_project_media(
        context[0], context[2], context[3], actor="fixture", store=context[-1]
    )
    run = preparation.start_preparation(
        context[0],
        context[2],
        StartMediaPreparation(
            expected_revision=2,
            idempotency_key="handoff-preparation",
            external_search_authorized=False,
        ),
        actor="fixture",
    )
    package = match(context, match_request(context, run))
    return context, package, choose(context, package)


def body(choice, **changes):
    return ApplyApprovedShot.model_validate(
        {
            "choice_id": choice["choice_id"],
            "expected_revision": 2,
            "expected_storyboard_version": 0,
            "beat_id": "beat-one",
            "idempotency_key": "apply",
            **changes,
        }
    )


def apply(context, choice, **changes):
    return apply_approved_shot(
        context[0], context[2], body(choice, **changes), actor="operator", store=context[-1]
    )


def plan(row):
    return StoryboardPlan.model_validate(
        {key: row.workspace[key] for key in ("presentation_mode", "beats", "narration_ids")}
    )


def test_handoff_replay_existing_renderer_contract_and_scope(handoff_context):
    context, _, choice = handoff_context
    result = apply(context, choice)
    assert result.version == 1 and result.workspace["asset_run_id"] is None
    assert apply(context, choice).version == 1
    identity = choice_media_id(uuid.UUID(choice["choice_id"]))
    assert result.workspace["beats"][0]["media"][0]["candidate_id"] == identity
    assert (
        result.workspace["beats"][0]["media"][0]["start_seconds"]
        == choice["binding"]["source_in_seconds"]
    )
    manifest = compile_project_visuals(context[0], context[2], 2, None, plan(result))
    assert (
        manifest.media[0].clip_id == str(context[4]) and manifest.media[0].candidate_id == identity
    )
    assert manifest.media[0].rights_assessment_id == str(choice["binding"]["rights_assessment_id"])
    assert manifest.timeline[0].duration_frames == 15 and manifest.version == "editorial-render-v6"
    run = start_run(
        context[0],
        context[2],
        StartEditorialRun(
            idempotency_key="bound-render",
            expected_revision=2,
            target="render",
            storyboard=plan(result),
        ),
        actor="operator",
    )
    assert run.options["asset_run_id"] is None
    with db.session_scope() as session:
        assert len(list(session.scalars(select(EditorialStoryboardRevision)))) == 1
        assert (
            session.get(OperatorShotChoice, uuid.UUID(choice["choice_id"])).payload["note"]
            == "Keep my selected shot"
        )
    for channel, project in ((context[1], context[2]), (context[0], uuid.uuid4())):
        with pytest.raises(ValueError, match="not found"):
            apply_approved_shot(channel, project, body(choice), actor="bad", store=context[-1])
    with pytest.raises(ValueError, match="different input"):
        apply(context, choice, freeze=True)


def test_no_proposal_or_unknown_rights_can_be_bound(handoff_context):
    context, _, choice = handoff_context
    with pytest.raises(ValueError, match="not found"):
        apply(context, choice, choice_id=uuid.uuid4())
    with db.session_scope() as session:
        session.get(ProjectMediaItem, context[3]).rights_status = "not_assessed"
    with pytest.raises(ValueError, match="stale|permission"):
        apply(context, choice)
    assert get_latest_storyboard(context[0], context[2]) is None


def test_manual_visual_protection_explicit_replace_and_history(handoff_context):
    context, _, choice = handoff_context
    current = apply(context, choice, freeze=True)
    original_media = current.workspace["beats"][0]["media"]
    with pytest.raises(ValueError, match="manual visual"):
        apply(context, choice, expected_storyboard_version=1, idempotency_key="replace")
    replaced = apply(
        context,
        choice,
        expected_storyboard_version=1,
        idempotency_key="replace",
        replace_existing=True,
    )
    assert replaced.version == 2 and replaced.parent_version == 1
    with db.session_scope() as session:
        assert (
            session.get(EditorialStoryboardRevision, (context[2], 2, 1)).workspace["beats"][0][
                "media"
            ]
            == original_media
        )
    with pytest.raises(ValueError, match="Storyboard changed"):
        apply(context, choice, idempotency_key="stale-version", replace_existing=True)


def test_replaced_choice_invalidates_compile(handoff_context):
    context, package, choice = handoff_context
    row = apply(context, choice)
    choose(context, package, key="new-choice", version=1, note="My explicit replacement")
    with pytest.raises(ValueError, match="replaced"):
        compile_project_visuals(context[0], context[2], 2, None, plan(row))
    with db.session_scope() as session:
        assert (
            session.get(OperatorShotChoice, uuid.UUID(choice["choice_id"])).payload["locked"]
            is True
        )


@pytest.mark.parametrize("change", ["rights", "bytes", "script", "source_metadata", "notes"])
def test_current_evidence_changes_stop_compile_and_preserve_workspace(handoff_context, change):
    context, _, choice = handoff_context
    row = apply(context, choice)
    with db.session_scope() as session:
        if change == "rights":
            assessment = session.get(
                RightsAssessment, uuid.UUID(choice["binding"]["rights_assessment_id"])
            )
            session.add(
                RightsAssessment(
                    discovery_candidate_id=assessment.discovery_candidate_id,
                    version=2,
                    production_eligible=False,
                )
            )
        elif change == "script":
            from katcha.editorial_models import EditorialProject

            session.get(EditorialProject, context[2]).revision = 3
        elif change == "notes":
            session.get(ProjectMediaItem, context[3]).notes = "Keep my revised manual annotation"
        elif change == "source_metadata":
            session.get(Clip, context[4]).sha256 = "f" * 64
        else:
            context[-1].data += b"tampered fixture"
    with pytest.raises(ValueError, match="stale|permission|changed|Draft|Script|bytes"):
        compile_project_visuals(context[0], context[2], 2, None, plan(row))
    assert (
        get_latest_storyboard(context[0], context[2], script_revision=2).workspace == row.workspace
    )


def test_playback_stays_within_measured_interval_and_hold_is_explicit(handoff_context):
    context, _, choice = handoff_context
    row = apply(context, choice)
    value = plan(row).model_dump(mode="json")
    use = value["beats"][0]["media"][0]
    use["start_seconds"] = 0.9
    with pytest.raises(ValueError, match="approved measured interval"):
        compile_project_visuals(
            context[0], context[2], 2, None, StoryboardPlan.model_validate(value)
        )
    use["freeze"] = True
    assert compile_project_visuals(
        context[0], context[2], 2, None, StoryboardPlan.model_validate(value)
    )
    use["start_seconds"] = 1.0
    with pytest.raises(ValueError, match="approved measured interval"):
        compile_project_visuals(
            context[0], context[2], 2, None, StoryboardPlan.model_validate(value)
        )


def test_render_rechecks_permission_after_output_before_promotion(handoff_context, monkeypatch):
    from katcha.editorial import render
    from katcha.rendering.client import RenderResult

    context, _, choice = handoff_context
    row = apply(context, choice)
    run = start_run(
        context[0],
        context[2],
        StartEditorialRun(
            idempotency_key="final-rights-render",
            expected_revision=2,
            target="render",
            storyboard=plan(row),
        ),
        actor="operator",
    )

    def fake_render(manifest):
        with db.session_scope() as session:
            session.get(ProjectMediaItem, context[3]).rights_status = "revoked"
        return RenderResult(
            output_key=manifest.output_key,
            metadata={"fixture": "mocked render"},
            duration_seconds=0.5,
        )

    monkeypatch.setattr(render, "render_editorial", fake_render)
    result = render.render_project(str(run.id), 1)
    assert result["status"] == "blocked"
    with db.session_scope() as session:
        stored = session.get(EditorialRun, run.id)
        assert stored.stage == "render_blocked"
        assert "render_result" in stored.artifacts and not stored.artifacts.get(
            "requires_editorial_review"
        )


def test_direct_workspace_save_rechecks_shot_and_rejects_arbitrary_storage(handoff_context):
    context, _, choice = handoff_context
    row = apply(context, choice)
    data = row.workspace
    with pytest.raises(ValueError):
        StoryboardWorkspace.model_validate({**data, "storage_key": "external/made-up.mp4"})
    with db.session_scope() as session:
        session.get(ProjectMediaItem, context[3]).rights_status = "revoked"
    with pytest.raises(ValueError, match="stale|permission"):
        save_storyboard(
            context[0],
            context[2],
            SaveStoryboardWorkspace(
                script_revision=2,
                expected_version=1,
                workspace=StoryboardWorkspace.model_validate(data),
                idempotency_key="edits",
            ),
            actor="operator",
        )
    assert get_latest_storyboard(context[0], context[2]).version == 1


@pytest.mark.parametrize("handoff_context", [True], indirect=True)
def test_other_beat_treatments_and_existing_narration_are_preserved(handoff_context):
    from katcha.editorial_models import EditorialNarration
    from katcha.services.editorial_projects import _digest

    context, _, choice = handoff_context
    narration_ids = {}
    with db.session_scope() as session:
        for beat, text in [("beat-one", "Look."), ("beat-two", "Hold.")]:
            identity = uuid.uuid4()
            narration_ids[beat] = identity
            session.add(
                EditorialNarration(
                    id=identity,
                    project_id=context[2],
                    channel_profile_id=context[0],
                    revision=2,
                    beat_id=beat,
                    text_digest=_digest({"text": text}),
                    sha256="a" * 64,
                    storage_key=f"editorial/{context[2]}/2/narration/{identity}/{'a' * 64}.wav",
                    sample_rate=16000,
                    sample_frames=8000,
                    actor="fixture",
                    status="active",
                )
            )
    original = StoryboardWorkspace.model_validate(
        {
            "presentation_mode": "narrated",
            "narration_ids": narration_ids,
            "beats": [
                {"beat_id": "beat-one"},
                {
                    "beat_id": "beat-two",
                    "layout": "single",
                    "media": [
                        {
                            "candidate_id": choice_media_id(uuid.UUID(choice["choice_id"])),
                            "freeze": True,
                        }
                    ],
                    "caption_position": "center",
                    "caption_scale": 1.2,
                    "transition": "fade",
                },
            ],
        }
    )
    previous = save_storyboard(
        context[0],
        context[2],
        SaveStoryboardWorkspace(
            script_revision=2,
            expected_version=0,
            workspace=original,
            idempotency_key="manual-baseline",
        ),
        actor="operator",
    )
    updated = apply(context, choice, expected_storyboard_version=1, idempotency_key="add-first")
    assert updated.workspace["beats"][1] == previous.workspace["beats"][1]
    assert updated.workspace["narration_ids"] == previous.workspace["narration_ids"]
    manifest = compile_project_visuals(context[0], context[2], 2, None, plan(updated))
    assert [audio.narration_id for audio in manifest.narration] == list(narration_ids.values())
    assert [scene.duration_frames for scene in manifest.timeline] == [15, 15]


def test_handoff_api_requires_write_and_channel_scope(handoff_context):
    from fastapi import HTTPException
    from starlette.requests import Request

    from katcha.api.editorial_shot_handoff import approved_shot

    context, _, choice = handoff_context
    http_request = Request({"type": "http", "headers": []})
    http_request.state.control_actor = "fixture"
    http_request.state.control_principal_name = "fixture"
    http_request.state.control_scopes = {"ai:read"}
    http_request.state.control_channel_profile_ids = {str(context[0])}
    with pytest.raises(HTTPException) as denied:
        approved_shot(context[0], context[2], body(choice), http_request)
    assert denied.value.status_code == 403
    http_request.state.control_scopes.add("production:create")
    output = approved_shot(context[0], context[2], body(choice), http_request)
    assert output.version == 1
    before = context[-1].calls
    with pytest.raises(HTTPException) as other_channel:
        approved_shot(context[1], context[2], body(choice), http_request)
    assert other_channel.value.status_code == 403 and context[-1].calls == before
    with pytest.raises(ValueError):
        ApplyApprovedShot.model_validate({**body(choice).model_dump(), "production_eligible": True})


def test_existing_review_pipeline_accepts_current_shots_and_invalidates_revocation(
    handoff_context, monkeypatch
):
    from katcha.editorial import render
    from katcha.editorial.review_schemas import ReviewEditorialRender
    from katcha.rendering.client import RenderResult
    from katcha.services.editorial_reviews import review_render, review_status

    context, _, choice = handoff_context
    workspace = apply(context, choice)
    run = start_run(
        context[0],
        context[2],
        StartEditorialRun(
            idempotency_key="review-compatible-render",
            expected_revision=2,
            target="render",
            storyboard=plan(workspace),
        ),
        actor="operator",
    )
    monkeypatch.setattr(
        render,
        "render_editorial",
        lambda manifest: RenderResult(
            output_key=manifest.output_key,
            duration_seconds=manifest.output_duration_seconds,
            metadata={"verified": True, "composition": "Editorial", "fixture": "mocked render"},
        ),
    )
    assert render.render_project(str(run.id), 1)["status"] == "completed"
    review = review_render(
        context[0],
        context[2],
        run.id,
        ReviewEditorialRender(
            expected_revision=2,
            expected_review_sequence=0,
            idempotency_key="approved-review",
            decision="approve",
            note="Fixture review",
        ),
        actor="operator",
    )
    assert review["decision"] == "approve"
    assert review_status(context[0], context[2], run.id)["publication_available"] is True
    with db.session_scope() as session:
        session.get(ProjectMediaItem, context[3]).rights_status = "revoked"
    status = review_status(context[0], context[2], run.id)
    assert status["status"] == "invalidated" and status["publication_available"] is False
    assert get_latest_storyboard(context[0], context[2]).version == 1
