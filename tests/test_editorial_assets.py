"""Synthetic asset discovery and current rights checks against actual persisted rows."""

import uuid

import pytest
from test_editorial_projects import draft
from test_editorial_projects import saved as _saved
from test_editorial_research import output
from test_editorial_runs import new_project

from katcha import db
from katcha.acquisition_models import DiscoveryCandidate, RightsAssessment
from katcha.editorial import assets, provider
from katcha.editorial.project_schemas import EditorialDraft, SaveEditorialDraft
from katcha.editorial.run_schemas import StartEditorialRun
from katcha.models import Clip, SourceItem
from katcha.orchestration import editorial_activities as activities
from katcha.orchestration import editorial_workflows as workflows
from katcha.services.editorial_projects import EditorialConflict, save_draft
from katcha.services.editorial_runs import get_run, start_run

saved = _saved


def scripted_project(saved):
    channel, project = new_project(saved)
    save_draft(
        channel,
        project,
        SaveEditorialDraft(
            expected_revision=0,
            idempotency_key="script",
            draft=EditorialDraft.model_validate(draft()),
        ),
        actor="test",
    )
    return channel, project


def test_scout_requires_saved_script(saved):
    channel, project = new_project(saved)
    with pytest.raises(EditorialConflict, match="Save a cited script"):
        start_run(
            channel,
            project,
            StartEditorialRun(
                expected_revision=0,
                idempotency_key="assets",
                target="assets",
            ),
            actor="test",
        )


async def test_asset_target_uses_frozen_script_and_grounded_leads_without_media_intake(
    saved, monkeypatch
):
    channel, project = scripted_project(saved)
    run = start_run(
        channel,
        project,
        StartEditorialRun(
            expected_revision=1,
            idempotency_key="assets",
            target="assets",
            max_queries=1,
        ),
        actor="test",
    )
    assert run.artifacts["input_draft"]["script"][0]["id"] == "beat-1"
    calls = []

    def invoke(route, prompt, schema, **kwargs):
        calls.append(schema.__name__)
        if schema.__name__ == "AssetPlan":
            return output(
                {
                    "requests": [
                        {
                            "id": "request",
                            "beat_id": "beat-1",
                            "claim_ids": ["claim-1"],
                            "purpose": "Compare the symbol",
                            "query": "Official symbol still",
                            "medium": "image",
                            "fallback": "source_frame",
                        }
                    ]
                }
            )
        response = output(
            {
                "leads": [
                    {
                        "url": "https://example.com/still",
                        "title": "Official still",
                        "medium": "image",
                        "relevance": "Shows the matching symbol",
                    },
                    {
                        "url": "https://invented.test/",
                        "title": "Invented",
                        "medium": "image",
                        "relevance": "Never accepted",
                    },
                ]
            }
        )
        response.grounded_urls = ["https://example.com/still"]
        return response

    monkeypatch.setattr(provider, "select_provider", lambda **kwargs: "codex")
    monkeypatch.setattr(provider, "_invoke", invoke)
    registry = {function.__name__: function for function in activities.EDITORIAL_ACTIVITIES}

    async def execute(name, *positional, args=None, **kwargs):
        assert name in {"editorial_begin", "editorial_scout_assets"}
        return registry[name](*(args if args is not None else positional))

    monkeypatch.setattr(workflows.workflow, "execute_activity", execute)
    result = await workflows.EditorialProjectWorkflow().run(str(run.id), 1)
    assert result["stage"] == "asset_candidates_ready"
    state = get_run(channel, project, run.id).artifacts["asset_scout"]
    assert len(state["candidates"]) == 1
    assert state["candidates"][0]["production_eligible"] is False
    assert state["candidates"][0]["acquired"] is False
    assert calls == ["AssetPlan", "AssetLeads"]


def test_rights_inspection_requires_same_channel_and_latest_clearance(saved):
    _, channel, other = saved
    candidate_id, clip_id, source_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    with db.session_scope() as session:
        session.add(
            Clip(
                id=clip_id,
                sha256="c" * 64,
                storage_key="raw/asset.mp4",
                duration_seconds=40,
                width=1920,
                height=1080,
                status="scored",
            )
        )
        session.add(
            SourceItem(
                id=source_id,
                source_url="https://example.com/asset",
                canonical_url="https://example.com/asset",
                platform="web",
                status="ready",
                clip_id=clip_id,
                source_metadata={"channel_profile_id": str(channel)},
            )
        )
        session.add(
            DiscoveryCandidate(
                id=candidate_id,
                source_item_id=source_id,
                adapter_key="manual",
                platform="web",
                source_url="https://example.com/asset",
                canonical_url="https://example.com/asset",
                candidate_metadata={"channel_profile_id": str(channel)},
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
    assert (
        assets.inspect_managed_candidate("https://example.com/asset", other)[
            "discovery_candidate_id"
        ]
        is None
    )
    value = assets.inspect_managed_candidate("https://example.com/asset", channel)
    assert value["production_eligible"] is True
    assert value["sha256"] == "c" * 64
    with db.session_scope() as session:
        session.add(
            RightsAssessment(
                discovery_candidate_id=candidate_id,
                version=2,
                production_eligible=False,
                actor="test",
            )
        )
    assert (
        assets.inspect_managed_candidate("https://example.com/asset", channel)[
            "production_eligible"
        ]
        is False
    )


def completed_scout(saved):
    from katcha.services.editorial_runs import checkpoint

    channel, project = scripted_project(saved)
    scout = start_run(
        channel,
        project,
        StartEditorialRun(
            expected_revision=1,
            idempotency_key="scout",
            target="assets",
        ),
        actor="test",
    )
    checkpoint(
        str(scout.id),
        1,
        status="completed",
        stage="asset_candidates_ready",
        artifacts={
            "asset_scout": {
                "requests": [],
                "candidates": [
                    {
                        "id": "asset",
                        "medium": "video",
                        "url": "https://www.youtube.com/watch?v=asset",
                        "title": "Supporting interview",
                        "beat_id": "beat-1",
                        "claim_ids": ["claim-1"],
                    }
                ],
            },
        },
    )
    return scout


def test_acquisition_selection_cannot_use_invented_candidates(saved):
    scout = completed_scout(saved)
    with pytest.raises(EditorialConflict, match="not discovered"):
        start_run(
            scout.channel_profile_id,
            scout.project_id,
            StartEditorialRun(
                expected_revision=1,
                idempotency_key="acquire",
                target="acquire_assets",
                scout_run_id=scout.id,
                asset_candidate_ids=["invented"],
            ),
            actor="test",
        )


async def test_asset_acquisition_reuses_managed_ingest_and_requires_rights_review(
    saved, monkeypatch
):
    scout = completed_scout(saved)
    row = start_run(
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
    registry = {function.__name__: function for function in activities.EDITORIAL_ACTIVITIES}
    calls = []

    async def execute(name, *positional, args=None, **kwargs):
        calls.append(name)
        values = args if args is not None else positional
        if name == "ingest_source":
            clip_id = uuid.uuid4()
            with db.session_scope() as session:
                session.add(
                    Clip(
                        id=clip_id,
                        sha256="e" * 64,
                        storage_key="raw/selected.mp4",
                        duration_seconds=35,
                        width=1920,
                        height=1080,
                        status="ingested",
                    )
                )
                source = session.get(SourceItem, uuid.UUID(values[0]))
                source.clip_id = clip_id
                source.status = "ready"
            return {"clip_id": str(clip_id)}
        return registry[name](*values)

    monkeypatch.setattr(workflows.workflow, "execute_activity", execute)
    result = await workflows.EditorialProjectWorkflow().run(str(row.id), 1)
    assert result["stage"] == "assets_acquired_for_review"
    current = get_run(row.channel_profile_id, row.project_id, row.id)
    assert current.artifacts["acquired_assets"]["asset"]["sha256"] == "e" * 64
    assert current.artifacts["requires_rights_review"] is True
    candidate = current.artifacts["asset_scout"]["candidates"][0]
    assert candidate["acquired"] is True
    assert candidate["production_eligible"] is False
    assert calls.count("ingest_source") == 1
    assert "build_local_intelligence" not in calls
    with db.session_scope() as session:
        assert session.query(RightsAssessment).count() == 0
