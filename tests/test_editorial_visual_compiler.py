"""Compiler contracts and authoritative media checks; no render-completion claim."""

import uuid

import pytest
from pydantic import ValidationError
from test_editorial_assets import completed_scout
from test_editorial_projects import draft
from test_editorial_projects import saved as _saved

from katcha import db
from katcha.acquisition_models import DiscoveryCandidate, RightsAssessment
from katcha.editorial.project_schemas import EditorialDraft
from katcha.editorial.run_schemas import StartEditorialRun
from katcha.editorial.visual_compiler import compile_project_visuals, compile_visuals
from katcha.editorial.visual_schemas import (
    EditorialRenderManifest,
    RenderMedia,
    StoryboardPlan,
)
from katcha.models import Clip, SourceItem
from katcha.services.editorial_projects import EditorialConflict
from katcha.services.editorial_runs import checkpoint, start_run

saved = _saved


def plan(**changes):
    beat = {"beat_id": "beat-1", "layout": "single", "media": [{"candidate_id": "asset"}]}
    beat.update(changes)
    return StoryboardPlan.model_validate({"presentation_mode": "captioned_silent", "beats": [beat]})


def media():
    return RenderMedia(
        candidate_id="asset",
        clip_id=str(uuid.uuid4()),
        storage_key="raw/asset.mp4",
        sha256="a" * 64,
        width=1920,
        height=1080,
        duration_seconds=90,
        rights_assessment_id=str(uuid.uuid4()),
    )


def compile_plan(storyboard, *, asset=None):
    return compile_visuals(
        project_id="project",
        revision=1,
        draft=EditorialDraft.model_validate(draft()),
        plan=storyboard,
        media=[asset or media()],
    )


def test_compiler_is_deterministic_and_preserves_uncertainty_and_frame_coverage():
    asset = media()
    first = compile_plan(plan(), asset=asset)
    second = compile_plan(plan(), asset=asset)
    assert first == second
    assert first.timeline[0].duration_frames == 240
    assert sum(c.duration_frames for c in first.timeline[0].captions) == 240
    assert first.timeline[0].uncertainty_disclosure == "Unconfirmed theory"
    assert first.requires_editorial_review is True
    assert first.version == "editorial-render-v1"
    assert first.output_key.endswith(".mp4")
    assert first.output_duration_seconds == 8


def test_professional_beat_controls_compile_only_into_v5():
    storyboard = plan(
        media=[
            {
                "candidate_id": "asset",
                "start_seconds": 1,
                "playback_rate": 0.75,
                "push_in": 1.08,
                "crop": {
                    "x": 0.1,
                    "y": 0.15,
                    "width": 0.7,
                    "height": 0.7,
                },
            }
        ],
        caption_position="center",
        caption_scale=1.15,
        caption_background=True,
        transition="fade",
        transition_frames=6,
    )
    manifest = compile_plan(storyboard)

    assert manifest.version == "editorial-render-v5"
    scene = manifest.timeline[0]
    assert scene.media[0].crop.x == 0.1
    assert scene.media[0].playback_rate == 0.75
    assert scene.media[0].push_in == 1.08
    assert scene.caption_position == "center"
    assert scene.caption_scale == 1.15
    assert scene.caption_background is True
    assert scene.transition == "fade"
    assert scene.transition_frames == 6
    assert scene.duration_frames == 240


    downgraded = manifest.model_dump(mode="json")
    downgraded["version"] = "editorial-render-v1"
    with pytest.raises(ValidationError, match="Professional beat controls"):
        EditorialRenderManifest.model_validate(downgraded)


@pytest.mark.parametrize(
    "change",
    [
        {
            "media": [
                {
                    "candidate_id": "asset",
                    "crop": {"x": 0.8, "y": 0, "width": 0.3, "height": 1},
                }
            ]
        },
        {"caption_scale": 2},
        {"transition": "fade", "transition_frames": 2},
    ],
)
def test_professional_beat_controls_reject_invalid_bounds(change):
    with pytest.raises(ValidationError):
        plan(**change)


def test_freeze_and_playback_have_distinct_source_time_bounds():
    with pytest.raises(ValidationError, match="source bounds"):
        compile_plan(plan(media=[{"candidate_id": "asset", "start_seconds": 89}]))
    result = compile_plan(
        plan(media=[{"candidate_id": "asset", "start_seconds": 89, "freeze": True}])
    )
    assert result.timeline[0].media[0].freeze is True


@pytest.mark.parametrize(
    "change",
    [
        {"beat_id": "invented"},
        {"layout": "quote", "media": [], "quote_source_id": "invented"},
    ],
)
def test_script_and_evidence_references_cannot_be_invented(change):
    with pytest.raises(EditorialConflict):
        compile_plan(plan(**change))


def test_quote_cards_copy_the_linked_excerpt():
    manifest = compile_plan(plan(layout="quote", media=[], quote_source_id="source-1"))
    assert manifest.timeline[0].quote_text == draft()["sources"][0]["excerpt"]
    assert manifest.timeline[0].source_credit == "Interview"


@pytest.mark.parametrize(
    "overlay",
    [
        {
            "kind": "javascript",
            "media_index": 0,
            "region": {"x": 0, "y": 0, "width": 1, "height": 1},
        },
        {"kind": "circle", "media_index": 1, "region": {"x": 0, "y": 0, "width": 1, "height": 1}},
        {
            "kind": "arrow",
            "media_index": 0,
            "region": {"x": 0.9, "y": 0, "width": 0.2, "height": 1},
        },
    ],
)
def test_invalid_annotation_primitives_and_bounds_fail(overlay):
    with pytest.raises(ValidationError):
        plan(overlays=[overlay])


def test_project_compiler_checks_current_rights_not_old_scout_flags(saved, monkeypatch):
    scout = completed_scout(saved)
    run = start_run(
        scout.channel_profile_id,
        scout.project_id,
        StartEditorialRun(
            expected_revision=1,
            idempotency_key="visual-acquisition",
            target="acquire_assets",
            scout_run_id=scout.id,
            asset_candidate_ids=["asset"],
        ),
        actor="test",
    )
    clip_id, source_id, candidate_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    source_url = "https://www.youtube.com/watch?v=asset"
    with db.session_scope() as session:
        session.add(
            Clip(
                id=clip_id,
                sha256="a" * 64,
                storage_key="raw/asset.mp4",
                duration_seconds=90,
                width=1920,
                height=1080,
                status="scored",
            )
        )
        session.add(
            SourceItem(
                id=source_id,
                source_url=source_url,
                canonical_url=source_url,
                platform="youtube",
                status="ready",
                clip_id=clip_id,
                source_metadata={"channel_profile_id": str(scout.channel_profile_id)},
            )
        )
        session.add(
            DiscoveryCandidate(
                id=candidate_id,
                source_item_id=source_id,
                source_url=source_url,
                canonical_url=source_url,
                platform="youtube",
                adapter_key="editorial_scout",
                candidate_metadata={"channel_profile_id": str(scout.channel_profile_id)},
            )
        )
        session.add(
            RightsAssessment(
                discovery_candidate_id=candidate_id,
                version=1,
                production_eligible=True,
                actor="reviewer",
            )
        )
    checkpoint(
        str(run.id),
        1,
        status="completed",
        artifacts={
            "acquired_assets": {
                "asset": {
                    "clip_id": str(clip_id),
                    "source_url": source_url,
                    "storage_key": "raw/asset.mp4",
                    "sha256": "a" * 64,
                    "duration_seconds": 90,
                }
            }
        },
    )
    value = compile_project_visuals(scout.channel_profile_id, scout.project_id, 1, run.id, plan())
    assert value.media[0].clip_id == str(clip_id)
    client = saved[0]
    url = (
        f"/v1/channels/{scout.channel_profile_id}/editorial-projects/"
        f"{scout.project_id}/storyboard/preflight"
    )
    body = {
        "expected_revision": 1,
        "asset_run_id": str(run.id),
        "plan": plan().model_dump(mode="json"),
    }
    response = client.post(url, json=body)
    assert response.status_code == 200, response.text
    assert response.json()["output_key"] == value.output_key
    assert response.json()["requires_editorial_review"] is True
    from fastapi.responses import Response

    from katcha.editorial.render import render_project
    from katcha.rendering.client import RenderResult

    rendering = start_run(
        scout.channel_profile_id,
        scout.project_id,
        StartEditorialRun(
            expected_revision=1,
            idempotency_key="actual-render-contract",
            target="render",
            asset_run_id=run.id,
            storyboard=plan(),
        ),
        actor="test",
    )
    monkeypatch.setattr(
        "katcha.editorial.render.render_editorial",
        lambda manifest: RenderResult(
            manifest.output_key,
            manifest.output_duration_seconds,
            {"verified": True, "composition": "Editorial"},
        ),
    )
    assert render_project(str(rendering.id), 1)["status"] == "completed"
    monkeypatch.setattr(
        "katcha.api.studio._stream_object",
        lambda request, key, filename: Response(key, media_type="video/mp4"),
    )
    preview_url = url.replace("/storyboard/preflight", f"/runs/{rendering.id}/preview")
    preview = client.get(preview_url)
    assert preview.status_code == 200
    assert preview.text == value.output_key
    review_url = preview_url.removesuffix("/preview") + "/review"
    review_request = {
        "idempotency_key": "review-real-manifest",
        "expected_revision": 1, "expected_review_sequence": 0,
        "decision": "approve", "note": "Synthetic review of compiled media",
    }
    approved = client.post(review_url, json=review_request)
    assert approved.status_code == 201, approved.text
    assert client.get(review_url).json()["status"] == "approve"
    with db.session_scope() as session:
        session.add(
            RightsAssessment(
                discovery_candidate_id=candidate_id,
                version=2,
                production_eligible=False,
                actor="reviewer",
            )
        )
    assert client.get(preview_url).status_code == 409
    assert client.get(review_url).json()["status"] == "invalidated"
    assert client.post(review_url, json={
        **review_request, "idempotency_key": "reapprove", "expected_review_sequence": 1,
    }).status_code == 409
    assert client.post(url, json=body).status_code == 409
    with pytest.raises(EditorialConflict, match="clearance"):
        compile_project_visuals(scout.channel_profile_id, scout.project_id, 1, run.id, plan())
