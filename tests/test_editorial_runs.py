import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from test_editorial_projects import brief, root
from test_editorial_projects import saved as _saved

from katcha import db
from katcha.editorial.run_schemas import StartEditorialRun
from katcha.editorial_models import EditorialRun
from katcha.models import Clip, ClipAnalysisRun, ClipFeature, SourceItem
from katcha.orchestration import editorial_activities as activities
from katcha.orchestration import editorial_dispatch as dispatch
from katcha.orchestration import editorial_workflows as workflows
from katcha.services.editorial_runs import (
    EditorialStopped,
    checkpoint,
    control_run,
    get_run,
    start_run,
)

saved = _saved


def new_project(saved):
    client, channel, _ = saved
    response = client.post(root(channel), json=brief())
    assert response.status_code == 201
    return channel, uuid.UUID(response.json()["id"])


def new_run(saved):
    channel, project = new_project(saved)
    return start_run(
        channel,
        project,
        StartEditorialRun(expected_revision=0, idempotency_key="run-1"),
        actor="test",
    )


async def test_api_dispatch_failure_preserves_intent_and_replay(saved, monkeypatch):
    client, channel, _ = saved
    _, project = new_project(saved)
    monkeypatch.setattr(
        "katcha.api.editorial_runs.get_temporal_client",
        AsyncMock(side_effect=RuntimeError("Temporal unavailable")),
    )
    url = f"{root(channel)}/{project}/runs"
    response = client.post(url, json={"expected_revision": 0, "idempotency_key": "r"})
    assert response.status_code == 202, response.text
    value = response.json()
    assert value["dispatch"] == "pending"
    assert value["status"] == "queued"
    assert (
        client.post(url, json={"expected_revision": 0, "idempotency_key": "r"}).json()[
            "editorial_run_id"
        ]
        == value["editorial_run_id"]
    )
    assert (
        client.post(url, json={"expected_revision": 0, "idempotency_key": "second"}).status_code
        == 409
    )
    assert len(client.get(url).json()) == 1
    temporal = SimpleNamespace(start_workflow=AsyncMock())
    assert await dispatch.reconcile_editorial_runs(temporal) == 1
    assert temporal.start_workflow.await_count == 1


def test_resume_preserves_checkpoint_and_fences_stale_completion(saved):
    row = new_run(saved)
    checkpoint(str(row.id), 1, artifacts={"source_snapshots": {"0": {"sha256": "verified"}}})
    checkpoint(str(row.id), 1, status="failed", error="Worker interrupted")
    resumed = control_run(
        row.channel_profile_id, row.project_id, row.id, expected_attempt=1, cancel=False
    )
    assert resumed.attempt == 2
    assert resumed.artifacts["source_snapshots"]["0"]["sha256"] == "verified"
    replay = control_run(
        row.channel_profile_id, row.project_id, row.id, expected_attempt=1, cancel=False
    )
    assert replay.attempt == 2
    with pytest.raises(EditorialStopped):
        checkpoint(str(row.id), 1, status="completed")
    control_run(row.channel_profile_id, row.project_id, row.id, expected_attempt=2, cancel=True)
    with pytest.raises(EditorialStopped):
        checkpoint(str(row.id), 2, status="completed")
    assert get_run(row.channel_profile_id, row.project_id, row.id).status == "cancelled"


async def test_cancelled_run_is_not_reconciled(saved):
    row = new_run(saved)
    control_run(row.channel_profile_id, row.project_id, row.id, expected_attempt=1, cancel=True)
    temporal = SimpleNamespace(start_workflow=AsyncMock())
    assert await dispatch.reconcile_editorial_runs(temporal) == 0
    temporal.start_workflow.assert_not_awaited()


async def test_real_activity_chain_reuses_managed_media_and_records_coverage(saved, monkeypatch):
    row = new_run(saved)
    clip_id, analysis_id = uuid.uuid4(), uuid.uuid4()
    with db.session_scope() as session:
        session.add(
            Clip(
                id=clip_id,
                sha256="a" * 64,
                storage_key="raw/fixture.mp4",
                duration_seconds=90,
                width=1920,
                height=1080,
                status="scored",
            )
        )
        session.add(
            SourceItem(
                source_url=row.artifacts["brief"]["source_urls"][0],
                canonical_url=row.artifacts["brief"]["source_urls"][0],
                platform="youtube",
                status="ready",
                clip_id=clip_id,
            )
        )
        session.add(
            ClipFeature(
                clip_id=clip_id,
                contact_sheet_key="analysis/sheet.jpg",
                local_features={"frame_count": 8},
                transcript="Synthetic transcript",
            )
        )
        session.add(
            ClipAnalysisRun(
                id=analysis_id,
                clip_id=clip_id,
                workflow_id="existing-analysis",
                status="completed",
                stage="completed",
            )
        )
    functions = {function.__name__: function for function in activities.EDITORIAL_ACTIVITIES}
    calls = []

    async def execute(name, *positional, args=None, **kwargs):
        calls.append(name)
        return functions[name](*(args if args is not None else positional))

    monkeypatch.setattr(workflows.workflow, "execute_activity", execute)
    result = await workflows.EditorialProjectWorkflow().run(str(row.id), 1)
    assert result["stage"] == "analysis_ready"
    assert "ingest_source" not in calls
    assert "build_local_intelligence" not in calls
    saved_run = get_run(row.channel_profile_id, row.project_id, row.id)
    source = saved_run.artifacts["source_snapshots"]["0"]
    assert source["analysis_run_id"] == str(analysis_id)
    assert source["sha256"] == "a" * 64
    assert source["coverage"] == "sampled_frames"
    assert source["native_video_analyzed"] is False


@pytest.mark.parametrize(
    "url",
    [
        "http://www.youtube.com/watch?v=a",
        "https://youtube.com.evil.test/watch?v=a",
        "https://127.0.0.1/video",
        "https://user:pass@youtube.com/watch?v=a",
        "https://youtube.com:8443/watch?v=a",
        "https://youtube.com/redirect?q=internal",
    ],
)
def test_automatic_intake_rejects_unsupported_targets(saved, url):
    client, channel, _ = saved
    body = brief()
    body["brief"]["source_urls"] = [url]
    project = client.post(root(channel), json=body).json()
    response = client.post(
        f"{root(channel)}/{project['id']}/runs",
        json={
            "expected_revision": 0,
            "idempotency_key": "intake",
        },
    )
    assert response.status_code == 409
    with db.session_scope() as session:
        assert session.query(EditorialRun).count() == 0
