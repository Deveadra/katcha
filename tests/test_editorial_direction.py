"""Synthetic model output exercises durable direction and authoritative compilation."""

import copy
import uuid
from unittest.mock import Mock

import pytest
from pydantic import ValidationError
from test_editorial_assets import completed_scout
from test_editorial_projects import saved as _saved
from test_editorial_research import output

from katcha import db
from katcha.editorial import direction, provider, source_monitor
from katcha.editorial.run_schemas import StartEditorialRun
from katcha.editorial.visual_schemas import DirectionResult
from katcha.editorial_models import EditorialProject
from katcha.orchestration import editorial_activities as activities
from katcha.orchestration import editorial_workflows as workflows
from katcha.services.editorial_projects import EditorialConflict
from katcha.services.editorial_runs import checkpoint, control_run, get_run, start_run

saved = _saved


def proposed(**changes):
    beat = {
        "beat_id": "beat-1",
        "layout": "quote",
        "media": [],
        "quote_source_id": "source-1",
        "rationale": "Show the linked interview evidence",
    }
    beat.update(changes)
    return {"beats": [beat]}


@pytest.fixture
def directing(saved, monkeypatch):
    scout = completed_scout(saved)
    acquired = start_run(
        scout.channel_profile_id,
        scout.project_id,
        StartEditorialRun(
            expected_revision=1,
            idempotency_key="acquire",
            target="acquire_assets",
            scout_run_id=scout.id,
            asset_candidate_ids=["asset"],
        ),
        actor="test",
    )
    checkpoint(str(acquired.id), 1, status="completed", artifacts={"acquired_assets": {}})
    request = StartEditorialRun(
        expected_revision=1,
        idempotency_key="direct",
        target="direction",
        asset_run_id=acquired.id,
        direction={"presentation_mode": "captioned_silent"},
    )
    row = start_run(scout.channel_profile_id, scout.project_id, request, actor="test")
    invoke = Mock(return_value=output(proposed()))
    monkeypatch.setattr(provider, "select_provider", lambda **kwargs: "codex")
    monkeypatch.setattr(provider, "_invoke", invoke)
    return row, invoke, request


def refresh(row):
    return get_run(row.channel_profile_id, row.project_id, row.id)


def resume(row):
    return control_run(
        row.channel_profile_id, row.project_id, row.id, expected_attempt=row.attempt, cancel=False
    )


async def test_workflow_builds_validated_storyboard_without_render_or_intake(
    directing, monkeypatch
):
    row, invoke, request = directing
    registry = {fn.__name__: fn for fn in activities.EDITORIAL_ACTIVITIES}

    async def execute(name, *args, **kwargs):
        assert name in {"editorial_begin", "editorial_direct_visuals"}
        if name == "editorial_direct_visuals":
            assert kwargs["retry_policy"].maximum_attempts == 1
        return registry[name](*kwargs.get("args", args))

    monkeypatch.setattr(workflows.workflow, "execute_activity", execute)
    assert (await workflows.EditorialProjectWorkflow().run(str(row.id), 1))["status"] == "completed"
    current = refresh(row)
    assert current.artifacts["storyboard"]["beats"][0]["quote_source_id"] == "source-1"
    assert current.artifacts["direction_duration_seconds"] == 8
    assert "render_dispatch" not in current.artifacts
    assert invoke.call_count == 1
    assert start_run(row.channel_profile_id, row.project_id, request, actor="test").id == row.id


def test_resume_recovers_receipt_after_compilation_failure(directing, monkeypatch):
    row, invoke, _ = directing
    real_compile = direction.compile_project_visuals
    compiler = Mock(side_effect=EditorialConflict("Clearance temporarily unavailable"))
    monkeypatch.setattr(direction, "compile_project_visuals", compiler)
    assert direction.direct_visuals(str(row.id), 1)["status"] == "blocked"
    assert refresh(row).artifacts["direction_proposal"]
    monkeypatch.setattr(direction, "compile_project_visuals", real_compile)
    resumed = resume(row)
    assert direction.direct_visuals(str(row.id), resumed.attempt)["status"] == "completed"
    assert invoke.call_count == 1


def test_timeout_blocks_duplicate_provider_call(directing):
    row, invoke, _ = directing
    invoke.side_effect = TimeoutError("lost response")
    assert direction.direct_visuals(str(row.id), 1)["status"] == "blocked"
    resumed = resume(row)
    assert direction.direct_visuals(str(row.id), resumed.attempt)["status"] == "blocked"
    assert "uncertain" in refresh(row).error
    assert invoke.call_count == 1


@pytest.mark.parametrize(
    "changes",
    [
        {"beat_id": "invented"},
        {"quote_source_id": "invented"},
        {"layout": "single", "quote_source_id": None, "media": [{"candidate_id": "invented"}]},
    ],
)
def test_model_cannot_invent_evidence_or_media(directing, changes):
    row, invoke, _ = directing
    invoke.return_value = output(proposed(**changes))
    assert direction.direct_visuals(str(row.id), 1)["status"] == "blocked"
    assert "storyboard" not in refresh(row).artifacts
    assert refresh(row).artifacts["direction_proposal"]


def test_stale_script_blocks_before_provider_call(directing):
    row, invoke, _ = directing
    with db.session_scope() as session:
        session.get(EditorialProject, row.project_id).revision = 2
    assert direction.direct_visuals(str(row.id), 1)["status"] == "blocked"
    invoke.assert_not_called()


def test_changed_script_during_model_call_cannot_promote(directing):
    row, invoke, _ = directing

    def changed(*args, **kwargs):
        with db.session_scope() as session:
            session.get(EditorialProject, row.project_id).revision = 2
        return output(proposed())

    invoke.side_effect = changed
    assert direction.direct_visuals(str(row.id), 1)["status"] == "blocked"
    assert "storyboard" not in refresh(row).artifacts


def test_cancel_during_model_call_cannot_promote(directing):
    row, invoke, _ = directing

    def cancelled(*args, **kwargs):
        control_run(row.channel_profile_id, row.project_id, row.id, expected_attempt=1, cancel=True)
        return output(proposed())

    invoke.side_effect = cancelled
    assert direction.direct_visuals(str(row.id), 1)["status"] == "stopped"
    assert refresh(row).status == "cancelled"


def test_direction_rejects_unknown_acquisition(directing):
    row, _, request = directing
    control_run(row.channel_profile_id, row.project_id, row.id, expected_attempt=1, cancel=True)
    invalid = request.model_copy(update={"idempotency_key": "other", "asset_run_id": uuid.uuid4()})
    with pytest.raises(EditorialConflict, match="completed asset acquisition"):
        start_run(row.channel_profile_id, row.project_id, invalid, actor="test")


def test_director_cannot_invent_spatial_annotations():
    with pytest.raises(ValidationError, match="observed source regions"):
        DirectionResult.model_validate(
            proposed(
                layout="single",
                quote_source_id=None,
                media=[{"candidate_id": "asset"}],
                overlays=[
                    {"kind": "circle", "region": {"x": 0, "y": 0, "width": 0.1, "height": 0.1}}
                ],
            )
        )


@pytest.mark.parametrize(
    "options",
    [
        {},
        {"presentation_mode": "narrated"},
        {"presentation_mode": "captioned_silent", "narration_ids": {"beat-1": str(uuid.uuid4())}},
    ],
)
def test_presentation_is_explicit_and_audio_cannot_be_silently_discarded(options):
    with pytest.raises(ValidationError):
        StartEditorialRun(
            expected_revision=1,
            idempotency_key="test",
            target="direction",
            asset_run_id=uuid.uuid4(),
            direction=options,
        )


def test_measured_narration_drives_direction(directing, monkeypatch):
    from test_editorial_narration import wav

    from katcha.editorial import narration

    row, invoke, request = directing
    control_run(row.channel_profile_id, row.project_id, row.id, expected_attempt=1, cancel=True)
    monkeypatch.setattr(narration, "ObjectStore", lambda: Mock())
    recording = narration.import_narration(
        row.channel_profile_id,
        row.project_id,
        revision=1,
        beat_id="beat-1",
        idempotency_key="audio",
        audio=wav(),
        actor="test",
        permitted_use=True,
    )
    voiced = request.model_copy(update={"idempotency_key": "voiced"})
    voiced = StartEditorialRun.model_validate(
        {
            **voiced.model_dump(mode="json"),
            "direction": {
                "presentation_mode": "narrated",
                "narration_ids": {
                    "beat-1": recording["id"],
                },
            },
        }
    )
    row = start_run(row.channel_profile_id, row.project_id, voiced, actor="test")
    assert direction.direct_visuals(str(row.id), 1)["status"] == "completed"
    assert refresh(row).artifacts["direction_duration_seconds"] == 61 / 30
    assert '"beat-1": 2.033333333333333' in invoke.call_args.args[1]
    assert refresh(row).artifacts["storyboard"]["narration_ids"]["beat-1"] == recording["id"]


def test_narration_removed_before_direction_does_not_call_provider(directing, monkeypatch):
    from test_editorial_narration import wav

    from katcha.editorial import narration

    row, invoke, request = directing
    control_run(row.channel_profile_id, row.project_id, row.id, expected_attempt=1, cancel=True)
    monkeypatch.setattr(narration, "ObjectStore", lambda: Mock())
    recording = narration.import_narration(
        row.channel_profile_id,
        row.project_id,
        revision=1,
        beat_id="beat-1",
        idempotency_key="audio",
        audio=wav(),
        actor="test",
        permitted_use=True,
    )
    narration.revoke_narration(
        row.channel_profile_id, row.project_id, uuid.UUID(recording["id"]), actor="test"
    )
    request = StartEditorialRun.model_validate(
        {
            **request.model_dump(mode="json"),
            "idempotency_key": "removed-audio",
            "direction": {
                "presentation_mode": "narrated",
                "narration_ids": {"beat-1": recording["id"]},
            },
        }
    )
    row = start_run(row.channel_profile_id, row.project_id, request, actor="test")
    assert direction.direct_visuals(str(row.id), 1)["status"] == "blocked"
    invoke.assert_not_called()


def test_duplicate_comparison_assets_are_rejected():
    with pytest.raises(ValidationError, match="distinct assets"):
        DirectionResult.model_validate(
            proposed(
                layout="comparison",
                quote_source_id=None,
                media=[{"candidate_id": "asset"}, {"candidate_id": "asset"}],
            )
        )


def install_media(row):
    from katcha.acquisition_models import DiscoveryCandidate, RightsAssessment
    from katcha.editorial_models import EditorialRun
    from katcha.models import Clip, SourceItem

    source_id, clip_id, candidate_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    url = "https://www.youtube.com/watch?v=asset"
    with db.session_scope() as session:
        session.add(
            Clip(
                id=clip_id,
                sha256="a" * 64,
                storage_key="raw/asset.mp4",
                extension="mp4",
                size_bytes=12_345,
                duration_seconds=10,
                width=1920,
                height=1080,
                status="scored",
            )
        )
        session.add(
            SourceItem(
                id=source_id,
                source_url=url,
                canonical_url=url,
                platform="youtube",
                status="ready",
                clip_id=clip_id,
                source_metadata={"channel_profile_id": str(row.channel_profile_id)},
            )
        )
        session.add(
            DiscoveryCandidate(
                id=candidate_id,
                source_item_id=source_id,
                source_url=url,
                canonical_url=url,
                platform="youtube",
                adapter_key="editorial_scout",
                candidate_metadata={"channel_profile_id": str(row.channel_profile_id)},
            )
        )
        session.add(
            RightsAssessment(
                discovery_candidate_id=candidate_id,
                version=1,
                production_eligible=True,
                actor="test",
            )
        )
        acquired = session.get(EditorialRun, uuid.UUID(row.options["asset_run_id"]))
        acquired.artifacts = {
            **acquired.artifacts,
            "acquired_assets": {
                "asset": {
                    "clip_id": str(clip_id),
                    "source_url": url,
                    "storage_key": "raw/asset.mp4",
                    "sha256": "a" * 64,
                    "duration_seconds": 10,
                }
            },
        }
    return candidate_id


@pytest.mark.parametrize("fault", ["none", "bounds", "rights", "hash"])
def test_generated_media_uses_real_compiler_clearance_and_bounds(directing, fault, monkeypatch):
    from katcha.acquisition_models import RightsAssessment
    from katcha.models import Clip

    row, invoke, _ = directing
    candidate_id = install_media(row)
    install_frames(monkeypatch)
    value = proposed(
        layout="single",
        quote_source_id=None,
        media=[{"candidate_id": "asset", "start_seconds": 9 if fault == "bounds" else 1}],
    )
    value["shot_evidence"] = [
        {"beat_id": "beat-1", "candidate_id": "asset", "observation_id": "frame-1"}
    ]

    def respond(*args, **kwargs):
        if kwargs.get("image"):
            return output(frame_observations())
        with db.session_scope() as session:
            if fault == "rights":
                session.add(
                    RightsAssessment(
                        discovery_candidate_id=candidate_id,
                        version=2,
                        production_eligible=False,
                        actor="test",
                    )
                )
            if fault == "hash":
                session.query(Clip).one().sha256 = "b" * 64
        return output(value)

    invoke.side_effect = respond
    expected = "completed" if fault == "none" else "blocked"
    assert direction.direct_visuals(str(row.id), 1)["status"] == expected, refresh(row).error
    assert invoke.call_count == 2
    assert ("storyboard" in refresh(row).artifacts) == (fault == "none")


def install_frames(monkeypatch):
    from katcha.models import Clip, ClipFeature

    with db.session_scope() as session:
        clip = session.query(Clip).one()
        session.add(
            ClipFeature(
                clip_id=clip.id,
                contact_sheet_key="analysis/asset/contact-sheet.jpg",
                keyframe_keys=["analysis/asset/frame.jpg"],
            )
        )
    monkeypatch.setattr(provider, "ObjectStore", lambda: Mock(get_bytes=Mock(return_value=b"jpeg")))


def install_monitor_frames():
    from katcha.models import Clip, ClipFeature

    with db.session_scope() as session:
        clip = session.query(Clip).one()
        prefix = f"analysis/{clip.sha256[:2]}/{clip.sha256}"
        session.add(
            ClipFeature(
                clip_id=clip.id,
                contact_sheet_key=f"{prefix}/contact-sheet.jpg",
                keyframe_keys=[
                    f"{prefix}/frames/frame-00.jpg",
                    f"{prefix}/frames/frame-01.jpg",
                    f"{prefix}/frames/frame-02.jpg",
                ],
            )
        )
        return clip.id


def test_storyboard_source_monitor_revalidates_acquired_frame_evidence(directing):
    row, _, _ = directing
    install_media(row)
    clip_id = install_monitor_frames()
    acquired_id = uuid.UUID(row.options["asset_run_id"])

    result = source_monitor.source_monitor(
        row.channel_profile_id,
        row.project_id,
        acquired_id,
        "asset",
    )

    assert result["candidate_id"] == "asset"
    assert result["clip_id"] == str(clip_id)
    assert result["sha256"] == "a" * 64
    assert result["duration_seconds"] == 10
    assert result["frame_count"] == 3
    assert result["sample_times"] == [0.25, 5.0, 9.75]
    assert result["coverage"] == "sampled_frames"
    assert result["contact_sheet_key"] == (f"analysis/aa/{'a' * 64}/contact-sheet.jpg")
    assert result["storage_key"] == "raw/asset.mp4"
    assert result["extension"] == "mp4"
    assert result["size_bytes"] == 12_345
    assert result["source_media_available"] is True


def test_storyboard_source_monitor_rejects_untrusted_frame_storage(directing):
    from katcha.models import ClipFeature

    row, _, _ = directing
    install_media(row)
    clip_id = install_monitor_frames()
    with db.session_scope() as session:
        session.get(ClipFeature, clip_id).contact_sheet_key = "analysis/shared/contact-sheet.jpg"

    with pytest.raises(EditorialConflict, match="invalid storage identity"):
        source_monitor.source_monitor(
            row.channel_profile_id,
            row.project_id,
            uuid.UUID(row.options["asset_run_id"]),
            "asset",
        )


def test_storyboard_source_monitor_requires_acquired_run(directing):
    row, _, _ = directing
    with pytest.raises(EditorialConflict, match="acquired-assets run"):
        source_monitor.source_monitor(
            row.channel_profile_id,
            row.project_id,
            row.id,
            "asset",
        )


def frame_observations(**changes):
    value = {
        "id": "frame-1",
        "source_url": "https://www.youtube.com/watch?v=asset",
        "source_duration_seconds": 10,
        "start_seconds": 5,
        "end_seconds": 6,
        "observation": "A red door is visible",
        "coverage": "sampled_frames",
    }
    value.update(changes)
    return {"observations": [value], "limitations": ["One sampled still; motion unknown"]}


def test_missing_supporting_frames_blocks_before_provider(directing):
    row, invoke, _ = directing
    install_media(row)
    assert direction.direct_visuals(str(row.id), 1)["status"] == "blocked"
    assert "sampled frames" in refresh(row).error
    invoke.assert_not_called()


@pytest.mark.parametrize(
    "changes",
    [
        {"source_url": "https://example.com/other"},
        {"source_duration_seconds": 11},
        {"start_seconds": 4},
    ],
)
def test_observer_rejects_fabricated_source_or_sample(directing, monkeypatch, changes):
    row, invoke, _ = directing
    install_media(row)
    install_frames(monkeypatch)
    invoke.return_value = output(frame_observations(**changes))
    assert direction.direct_visuals(str(row.id), 1)["status"] == "blocked"
    assert "source or sample time" in refresh(row).error
    assert invoke.call_count == 1
    assert "storyboard" not in refresh(row).artifacts


def test_frame_evidence_replays_without_provider_or_storage(directing, monkeypatch):
    row, invoke, _ = directing
    install_media(row)
    install_frames(monkeypatch)
    value = proposed(
        layout="single",
        quote_source_id=None,
        media=[{"candidate_id": "asset", "start_seconds": 1}],
    )
    value["shot_evidence"] = [
        {"beat_id": "beat-1", "candidate_id": "asset", "observation_id": "frame-1"}
    ]
    invoke.side_effect = [output(frame_observations()), output(value)]
    real_compile = direction.compile_project_visuals
    monkeypatch.setattr(
        direction, "compile_project_visuals", Mock(side_effect=EditorialConflict("retry"))
    )
    assert direction.direct_visuals(str(row.id), 1)["status"] == "blocked"
    evidence = refresh(row).artifacts["direction_evidence"]["asset"]
    assert evidence["observations"][0]["end_seconds"] == 5.001
    assert "red door" in invoke.call_args.args[1]
    monkeypatch.setattr(provider, "ObjectStore", Mock(side_effect=AssertionError("no download")))
    monkeypatch.setattr(direction, "compile_project_visuals", real_compile)
    resumed = resume(row)
    assert direction.direct_visuals(str(row.id), resumed.attempt)["status"] == "completed"
    assert invoke.call_count == 2
    current = refresh(row)
    from katcha.services.editorial_projects import _digest

    assert current.artifacts["direction_shot_evidence"]["storyboard_digest"] == _digest(
        current.artifacts["storyboard"]
    )
    assert (
        current.artifacts["direction_shot_evidence"]["shots"][0]["observation"]["id"] == "frame-1"
    )


def test_uncertain_observation_cannot_be_repeated(directing, monkeypatch):
    row, invoke, _ = directing
    install_media(row)
    install_frames(monkeypatch)
    invoke.side_effect = TimeoutError("response lost")
    assert direction.direct_visuals(str(row.id), 1)["status"] == "blocked"
    resumed = resume(row)
    assert direction.direct_visuals(str(row.id), resumed.attempt)["status"] == "blocked"
    assert invoke.call_count == 1
    assert "uncertain" in refresh(row).error


def test_legacy_direction_receipt_resumes_without_new_observation(directing, monkeypatch):
    row, invoke, _ = directing
    install_media(row)
    real_observe = direction.observe_direction_assets
    monkeypatch.setattr(direction, "observe_direction_assets", lambda *args: {})
    real_call = direction.structured_call

    def legacy_call(run_id, attempt, key, *args, **kwargs):
        prompt = args[0].split("\nFor every selected footage use,")[0]
        return real_call(run_id, attempt, "visual-direction-v1", prompt, DirectionResult, **kwargs)

    monkeypatch.setattr(direction, "structured_call", legacy_call)
    real_compile = direction.compile_project_visuals
    monkeypatch.setattr(
        direction, "compile_project_visuals", Mock(side_effect=EditorialConflict("retry"))
    )
    assert direction.direct_visuals(str(row.id), 1)["status"] == "blocked"
    monkeypatch.setattr(direction, "observe_direction_assets", real_observe)
    monkeypatch.setattr(direction, "structured_call", real_call)
    monkeypatch.setattr(direction, "compile_project_visuals", real_compile)
    resumed = resume(row)
    assert direction.direct_visuals(str(row.id), resumed.attempt)["status"] == "completed"
    assert invoke.call_count == 1


def test_changed_media_blocks_before_observation(directing, monkeypatch):
    from katcha.models import Clip

    row, invoke, _ = directing
    install_media(row)
    install_frames(monkeypatch)
    with db.session_scope() as session:
        session.query(Clip).one().sha256 = "b" * 64
    assert direction.direct_visuals(str(row.id), 1)["status"] == "blocked"
    invoke.assert_not_called()


def test_script_change_during_observation_blocks_planning_call(directing, monkeypatch):
    row, invoke, _ = directing
    install_media(row)
    install_frames(monkeypatch)

    def changed(*args, **kwargs):
        with db.session_scope() as session:
            session.get(EditorialProject, row.project_id).revision = 2
        return output(frame_observations())

    invoke.side_effect = changed
    assert direction.direct_visuals(str(row.id), 1)["status"] == "blocked"
    assert invoke.call_count == 1
    assert "storyboard" not in refresh(row).artifacts


@pytest.mark.parametrize(
    "fault",
    [
        "none",
        "missing",
        "unknown",
        "duplicate",
        "wrong-beat",
        "outside",
        "freeze",
        "freeze-match",
        "empty",
        "ambiguous",
    ],
)
def test_shot_references_are_bound_to_observed_playback(directing, monkeypatch, fault):
    row, invoke, _ = directing
    install_media(row)
    install_frames(monkeypatch)
    value = proposed(
        layout="single",
        quote_source_id=None,
        media=[
            {
                "candidate_id": "asset",
                "start_seconds": 5 if fault == "freeze-match" else 1,
                "freeze": fault in {"freeze", "freeze-match"},
                "playback_rate": 0.25 if fault == "outside" else 1,
            }
        ],
    )
    reference = {"beat_id": "beat-1", "candidate_id": "asset", "observation_id": "frame-1"}
    if fault == "unknown":
        reference["observation_id"] = "invented"
    if fault == "wrong-beat":
        reference["beat_id"] = "other"
    value["shot_evidence"] = [] if fault == "missing" else [reference]
    if fault == "duplicate":
        value["shot_evidence"].append(reference)
    observations = frame_observations()
    if fault == "empty":
        observations["observations"] = []
    if fault == "ambiguous":
        observations["observations"] *= 2
    invoke.side_effect = [output(observations), output(value)]
    success = fault in {"none", "freeze-match"}
    assert direction.direct_visuals(str(row.id), 1)["status"] == (
        "completed" if success else "blocked"
    ), refresh(row).error
    current = refresh(row)
    assert ("storyboard" in current.artifacts) == success
    if success:
        shot = current.artifacts["direction_shot_evidence"]["shots"][0]
        assert shot["sha256"] == "a" * 64
        assert shot["observation"]["id"] == "frame-1"
        assert shot["observation"]["start_seconds"] == 5
    else:
        resumed = resume(row)
        assert direction.direct_visuals(str(row.id), resumed.attempt)["status"] == "blocked"
        assert invoke.call_count == 2


def test_quote_cannot_claim_unselected_frame_evidence(directing):
    row, invoke, _ = directing
    invoke.return_value = output(
        {
            **proposed(),
            "shot_evidence": [
                {"beat_id": "beat-1", "candidate_id": "asset", "observation_id": "frame-1"}
            ],
        }
    )
    assert direction.direct_visuals(str(row.id), 1)["status"] == "blocked"
    assert "selected footage" in refresh(row).error


def test_v2_receipt_keeps_original_schema_and_prompt_on_resume(directing, monkeypatch):
    row, invoke, _ = directing
    install_media(row)
    install_frames(monkeypatch)
    invoke.side_effect = [output(frame_observations()), output(proposed())]
    real_call = direction.structured_call
    real_compile = direction.compile_project_visuals

    def v2_call(run_id, attempt, key, prompt, schema, **kwargs):
        if key == "visual-direction-v3":
            key = "visual-direction-v2"
            prompt = prompt.split("\nFor every selected footage use,")[0]
            schema = DirectionResult
        return real_call(run_id, attempt, key, prompt, schema, **kwargs)

    monkeypatch.setattr(direction, "structured_call", v2_call)
    monkeypatch.setattr(
        direction, "compile_project_visuals", Mock(side_effect=EditorialConflict("retry"))
    )
    assert direction.direct_visuals(str(row.id), 1)["status"] == "blocked"
    monkeypatch.setattr(direction, "structured_call", real_call)
    monkeypatch.setattr(direction, "compile_project_visuals", real_compile)
    monkeypatch.setattr(provider, "ObjectStore", Mock(side_effect=AssertionError("no download")))
    resumed = resume(row)
    assert direction.direct_visuals(str(row.id), resumed.attempt)["status"] == "completed"
    assert invoke.call_count == 2
    assert "direction_shot_evidence" not in refresh(row).artifacts


@pytest.fixture
def directed_render(directing, monkeypatch):
    row, invoke, _ = directing
    install_media(row)
    install_frames(monkeypatch)
    value = proposed(
        layout="single",
        quote_source_id=None,
        media=[{"candidate_id": "asset", "start_seconds": 1}],
    )
    value["shot_evidence"] = [
        {"beat_id": "beat-1", "candidate_id": "asset", "observation_id": "frame-1"}
    ]
    invoke.side_effect = [output(frame_observations()), output(value)]
    assert direction.direct_visuals(str(row.id), 1)["status"] == "completed"
    row = refresh(row)
    request = StartEditorialRun(
        target="render",
        expected_revision=1,
        idempotency_key="directed-preview",
        direction_run_id=row.id,
        asset_run_id=row.options["asset_run_id"],
        storyboard=row.artifacts["storyboard"],
    )
    return row, request


def test_directed_preview_preserves_citations_and_replays(directed_render):
    from katcha.editorial.render import current_manifest

    row, request = directed_render
    rendered = start_run(row.channel_profile_id, row.project_id, request, actor="test")
    assert rendered.artifacts["direction_shot_evidence"] == row.artifacts["direction_shot_evidence"]
    assert rendered.artifacts["direction_run_id"] == str(row.id)
    assert current_manifest(rendered).timeline[0].media[0].start_seconds == 1
    assert (
        start_run(row.channel_profile_id, row.project_id, request, actor="test").id == rendered.id
    )


@pytest.mark.parametrize(
    "change", ["trim", "asset", "missing", "project", "channel", "revision", "status", "digest"]
)
def test_directed_preview_rejects_mismatched_plan(directed_render, change):
    from katcha.editorial_models import EditorialRun

    row, request = directed_render
    if change == "trim":
        request.storyboard.beats[0].media[0].start_seconds = 2
    elif change == "asset":
        request.asset_run_id = uuid.uuid4()
    elif change == "missing":
        request.direction_run_id = uuid.uuid4()
    else:
        with db.session_scope() as session:
            stored = session.get(EditorialRun, row.id)
            if change == "project":
                stored.project_id = uuid.uuid4()
            elif change == "channel":
                stored.channel_profile_id = uuid.uuid4()
            elif change == "revision":
                stored.input_revision = 2
            elif change == "status":
                stored.status = "blocked"
            else:
                stored.artifacts = {
                    **stored.artifacts,
                    "direction_shot_evidence": {
                        **stored.artifacts["direction_shot_evidence"],
                        "storyboard_digest": "0" * 64,
                    },
                }
    with pytest.raises(EditorialConflict):
        start_run(row.channel_profile_id, row.project_id, request, actor="test")


def test_directed_preview_rechecks_evidence_before_render_and_review(directed_render):
    from katcha.editorial.render import current_manifest
    from katcha.editorial_models import EditorialRun

    row, request = directed_render
    rendered = start_run(row.channel_profile_id, row.project_id, request, actor="test")
    with db.session_scope() as session:
        stored = session.get(EditorialRun, row.id)
        stored.artifacts = {
            **stored.artifacts,
            "direction_shot_evidence": {
                **stored.artifacts["direction_shot_evidence"],
                "shots": [],
            },
        }
    with pytest.raises(EditorialConflict, match="citations changed"):
        current_manifest(rendered)


def test_legacy_plan_can_render_without_claiming_frame_citations(directing):
    row, _, _ = directing
    assert direction.direct_visuals(str(row.id), 1)["status"] == "completed"
    row = refresh(row)
    from katcha.editorial_models import EditorialRun

    with db.session_scope() as session:
        stored = session.get(EditorialRun, row.id)
        stored.artifacts = {
            key: value
            for key, value in stored.artifacts.items()
            if key != "direction_shot_evidence"
        }
    request = StartEditorialRun(
        target="render",
        expected_revision=1,
        idempotency_key="legacy-preview",
        direction_run_id=row.id,
        asset_run_id=row.options["asset_run_id"],
        storyboard=row.artifacts["storyboard"],
    )
    rendered = start_run(row.channel_profile_id, row.project_id, request, actor="test")
    assert "direction_shot_evidence" not in rendered.artifacts


def test_native_direction_selection_requires_observed_identity(monkeypatch):
    from types import SimpleNamespace

    from katcha.services import goal_tools

    identity = str(uuid.uuid4())
    goal = SimpleNamespace(channel_profile_id=uuid.uuid4())
    monkeypatch.setattr(goal_tools, "_known_ids", lambda _: set())
    with pytest.raises(ValueError, match="not observed"):
        goal_tools.validate_resource_arguments(goal, {"body": {"direction_run_id": identity}})
    monkeypatch.setattr(goal_tools, "_known_ids", lambda _: {identity})
    goal_tools.validate_resource_arguments(goal, {"body": {"direction_run_id": identity}})


def test_direction_receipt_is_only_valid_for_rendering():
    with pytest.raises(ValidationError, match="only be selected for rendering"):
        StartEditorialRun(
            target="analysis",
            expected_revision=0,
            idempotency_key="bad-reference",
            direction_run_id=uuid.uuid4(),
        )


@pytest.fixture
def region_direction(directing, monkeypatch):
    from katcha.editorial.visual_schemas import DirectionOptions
    from katcha.models import Clip, ClipFeature

    old, invoke, request = directing
    control_run(old.channel_profile_id, old.project_id, old.id, expected_attempt=1, cancel=True)
    row = start_run(
        old.channel_profile_id,
        old.project_id,
        request.model_copy(
            update={
                "idempotency_key": "regions",
                "direction": DirectionOptions(
                    presentation_mode="captioned_silent",
                    annotate_regions=True,
                ),
            }
        ),
        actor="test",
    )
    install_media(row)
    install_frames(monkeypatch)
    with db.session_scope() as session:
        clip = session.query(Clip).one()
        features = session.get(ClipFeature, clip.id)
        features.keyframe_keys = [f"analysis/{clip.sha256[:2]}/{clip.sha256}/frames/frame-00.jpg"]
    proposal = proposed(
        layout="single",
        quote_source_id=None,
        media=[
            {
                "candidate_id": "asset",
                "start_seconds": 5,
                "freeze": True,
            }
        ],
    )
    proposal["shot_evidence"] = [
        {"beat_id": "beat-1", "candidate_id": "asset", "observation_id": "frame-1"}
    ]
    regions = {
        "regions": [
            {
                "kind": "circle",
                "region": {
                    "x": 0.1,
                    "y": 0.2,
                    "width": 0.3,
                    "height": 0.4,
                },
                "description": "The visible red door",
                "label": "Door",
            }
        ],
        "limitations": ["Identity uncertain"],
    }
    invoke.side_effect = [output(frame_observations()), output(proposal), output(regions)]
    return row, invoke, proposal, regions


def test_opt_in_regions_inspect_exact_frame_and_preserve_render_receipt(
    region_direction, monkeypatch
):
    row, invoke, _, _ = region_direction
    store = Mock(get_bytes=Mock(return_value=b"jpeg"))
    monkeypatch.setattr(provider, "ObjectStore", lambda: store)
    assert direction.direct_visuals(str(row.id), 1)["status"] == "completed", refresh(row).error
    current = refresh(row)
    overlay = current.artifacts["storyboard"]["beats"][0]["overlays"][0]
    assert overlay["kind"] == "circle" and overlay["media_index"] == 0
    assert overlay["region"]["width"] == 0.3
    assert store.get_bytes.call_args_list[-1].args[0].endswith("/frames/frame-00.jpg")
    assert "ONE full source frame" in invoke.call_args.args[1]
    assert invoke.call_count == 3
    evidence = current.artifacts["direction_shot_evidence"]
    assert evidence["shots"][0]["regions"]["regions"][0]["description"] == "The visible red door"
    rendered = start_run(
        row.channel_profile_id,
        row.project_id,
        StartEditorialRun(
            target="render",
            expected_revision=1,
            idempotency_key="region-render",
            asset_run_id=row.options["asset_run_id"],
            direction_run_id=row.id,
            storyboard=current.artifacts["storyboard"],
        ),
        actor="test",
    )
    from katcha.editorial.render import current_manifest

    assert current_manifest(rendered).timeline[0].overlays[0].kind == "circle"
    assert rendered.artifacts["direction_shot_evidence"] == evidence


def test_region_receipts_recover_without_repeat_provider_charge(region_direction, monkeypatch):
    row, invoke, _, _ = region_direction
    real_compile = direction.compile_project_visuals
    calls = 0

    def compile_once(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise EditorialConflict("Interrupted promotion")
        return real_compile(*args, **kwargs)

    monkeypatch.setattr(direction, "compile_project_visuals", compile_once)
    assert direction.direct_visuals(str(row.id), 1)["status"] == "blocked"
    assert invoke.call_count == 3
    monkeypatch.setattr(provider, "ObjectStore", Mock(side_effect=AssertionError("no download")))
    resumed = resume(row)
    assert direction.direct_visuals(str(row.id), resumed.attempt)["status"] == "completed"
    assert invoke.call_count == 3


def test_unknown_region_response_blocks_automatic_retry(region_direction):
    row, invoke, proposal, _ = region_direction
    invoke.side_effect = [output(frame_observations()), output(proposal), TimeoutError("lost")]
    assert direction.direct_visuals(str(row.id), 1)["status"] == "blocked"
    assert "storyboard" not in refresh(row).artifacts
    resumed = resume(row)
    assert direction.direct_visuals(str(row.id), resumed.attempt)["status"] == "blocked"
    assert invoke.call_count == 3


@pytest.mark.parametrize("fault", ["bounds", "key", "changed_key", "revision", "coverage"])
def test_region_failures_do_not_promote_plan(region_direction, monkeypatch, fault):
    from katcha.models import ClipFeature

    row, invoke, proposal, regions = region_direction
    if fault == "bounds":
        regions["regions"][0]["region"]["x"] = 0.9
    if fault == "key":
        with db.session_scope() as session:
            session.query(ClipFeature).one().keyframe_keys = ["private/unrelated.jpg"]
    calls = 0

    def respond(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return output(frame_observations())
        if calls == 2:
            return output(proposal)
        if fault == "changed_key":
            with db.session_scope() as session:
                session.query(ClipFeature).one().keyframe_keys = ["other.jpg"]
        if fault == "revision":
            with db.session_scope() as session:
                session.get(EditorialProject, row.project_id).revision = 2
        return output(regions)

    invoke.side_effect = respond
    if fault == "coverage":
        from katcha.editorial import regions as module

        original = module.structured_call

        def wrong_coverage(*args, **kwargs):
            result, receipt = original(*args, **kwargs)
            return result, {**receipt, "coverage": "text"}

        monkeypatch.setattr(module, "structured_call", wrong_coverage)
    assert direction.direct_visuals(str(row.id), 1)["status"] == "blocked"
    assert "storyboard" not in refresh(row).artifacts
    assert invoke.call_count == (2 if fault == "key" else 3)


@pytest.mark.parametrize("freeze,empty", [(False, False), (True, True)])
def test_no_forced_annotations_on_motion_or_uncertain_frames(region_direction, freeze, empty):
    row, invoke, proposal, regions = region_direction
    proposal["beats"][0]["media"][0]["freeze"] = freeze
    if not freeze:
        proposal["beats"][0]["media"][0]["start_seconds"] = 1
    if empty:
        regions["regions"] = []
    invoke.side_effect = [output(frame_observations()), output(proposal), output(regions)]
    assert direction.direct_visuals(str(row.id), 1)["status"] == "completed", refresh(row).error
    assert not refresh(row).artifacts["storyboard"]["beats"][0]["overlays"]
    assert invoke.call_count == (3 if freeze else 2)


def test_region_option_preserves_old_request_serialization():
    from katcha.editorial.visual_schemas import DirectionOptions

    options = DirectionOptions(presentation_mode="captioned_silent")
    assert options.model_dump() == {"presentation_mode": "captioned_silent", "narration_ids": {}}


def test_region_frame_must_match_rendered_freeze_frame(region_direction):
    from katcha.editorial.regions import frame_input

    row, _, _, _ = region_direction
    with pytest.raises(provider.EditorialBlocked, match="exact sampled source frame"):
        frame_input(row, "asset", 4.9999)


def test_region_storage_failure_can_resume_before_dispatch(region_direction, monkeypatch):
    row, invoke, _, _ = region_direction
    reads = 0

    def get_bytes(key):
        nonlocal reads
        reads += 1
        if "/frames/" in key and reads == 2:
            raise OSError("temporary image storage failure")
        return b"jpeg"

    monkeypatch.setattr(provider, "ObjectStore", lambda: Mock(get_bytes=get_bytes))
    assert direction.direct_visuals(str(row.id), 1)["status"] == "blocked"
    assert invoke.call_count == 2
    resumed = resume(row)
    assert direction.direct_visuals(str(row.id), resumed.attempt)["status"] == "completed"
    assert invoke.call_count == 3


def test_region_clearance_change_during_observation_blocks_promotion(region_direction):
    from katcha.acquisition_models import DiscoveryCandidate, RightsAssessment

    row, invoke, proposal, regions = region_direction
    count = 0

    def respond(*args, **kwargs):
        nonlocal count
        count += 1
        if count == 1:
            return output(frame_observations())
        if count == 2:
            return output(proposal)
        with db.session_scope() as session:
            candidate = session.query(DiscoveryCandidate).one()
            session.add(
                RightsAssessment(
                    discovery_candidate_id=candidate.id,
                    version=2,
                    production_eligible=False,
                    actor="test",
                )
            )
        return output(regions)

    invoke.side_effect = respond
    assert direction.direct_visuals(str(row.id), 1)["status"] == "blocked"
    assert "clearance" in refresh(row).error
    assert "storyboard" not in refresh(row).artifacts


def test_cited_frame_inspection_revalidates_saved_evidence(region_direction):
    from katcha.editorial.frame_inspection import inspect_frame
    from katcha.editorial_models import EditorialRun

    row, invoke, _, _ = region_direction
    assert direction.direct_visuals(str(row.id), 1)["status"] == "completed"
    info = inspect_frame(row.channel_profile_id, row.project_id, row.id, 0)
    assert info["sample_seconds"] == 5
    assert info["image_key"].endswith("/frames/frame-00.jpg")
    assert info["overlays"][0]["region"]["width"] == 0.3
    assert info["frozen"] is True
    assert len(info["evidence_digest"]) == 64
    assert invoke.call_count == 3  # Inspection does not call a provider.
    with pytest.raises(EditorialConflict):
        inspect_frame(row.channel_profile_id, row.project_id, row.id, -1)
    with db.session_scope() as session:
        stored = session.get(EditorialRun, row.id)
        artifacts = dict(stored.artifacts)
        proof = copy.deepcopy(artifacts["direction_shot_evidence"])
        proof["shots"][0]["observation"]["observation"] = "Changed evidence"
        stored.artifacts = {**artifacts, "direction_shot_evidence": proof}
    with pytest.raises(EditorialConflict, match="evidence changed"):
        inspect_frame(row.channel_profile_id, row.project_id, row.id, 0)


def test_cited_frame_inspection_rechecks_current_rights(region_direction):
    from katcha.acquisition_models import DiscoveryCandidate, RightsAssessment
    from katcha.editorial.frame_inspection import inspect_frame

    row, _, _, _ = region_direction
    assert direction.direct_visuals(str(row.id), 1)["status"] == "completed"
    with db.session_scope() as session:
        candidate = session.query(DiscoveryCandidate).one()
        session.add(
            RightsAssessment(
                discovery_candidate_id=candidate.id,
                version=2,
                production_eligible=False,
                actor="test",
            )
        )
    with pytest.raises(ValueError, match="clearance|eligible"):
        inspect_frame(row.channel_profile_id, row.project_id, row.id, 0)


def test_cited_frame_inspection_from_saved_render(region_direction):
    from katcha.editorial.frame_inspection import inspect_frame
    from katcha.services.editorial_projects import EditorialNotFound

    row, _, _, _ = region_direction
    assert direction.direct_visuals(str(row.id), 1)["status"] == "completed"
    rendered = start_run(
        row.channel_profile_id,
        row.project_id,
        StartEditorialRun(
            target="render",
            expected_revision=1,
            idempotency_key="inspect-render",
            asset_run_id=row.options["asset_run_id"],
            direction_run_id=row.id,
            storyboard=refresh(row).artifacts["storyboard"],
        ),
        actor="test",
    )
    checkpoint(str(rendered.id), 1, status="completed")
    assert inspect_frame(row.channel_profile_id, row.project_id, rendered.id, 0)["frozen"]
    with pytest.raises(EditorialNotFound):
        inspect_frame(uuid.uuid4(), row.project_id, rendered.id, 0)
