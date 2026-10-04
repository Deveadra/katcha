"""Synthetic model output exercises durable direction and authoritative compilation."""

import uuid
from unittest.mock import Mock

import pytest
from pydantic import ValidationError
from test_editorial_assets import completed_scout
from test_editorial_projects import saved as _saved
from test_editorial_research import output

from katcha import db
from katcha.editorial import direction, provider
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
def test_generated_media_uses_real_compiler_clearance_and_bounds(directing, fault):
    from katcha.acquisition_models import RightsAssessment
    from katcha.models import Clip

    row, invoke, _ = directing
    candidate_id = install_media(row)
    value = proposed(
        layout="single",
        quote_source_id=None,
        media=[{"candidate_id": "asset", "start_seconds": 9 if fault == "bounds" else 1}],
    )

    def respond(*args, **kwargs):
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
    assert invoke.call_count == 1
    assert ("storyboard" in refresh(row).artifacts) == (fault == "none")
