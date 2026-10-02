from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from katcha.orchestration import (
    analysis_worker,
    intelligence_worker,
    longform_worker,
    production_worker,
    worker,
)
from katcha.orchestration import (
    client as orchestration_client,
)


class _FakeSession:
    def __init__(self, batches):
        self._batches = iter(batches)

    def scalars(self, _statement):
        return iter(next(self._batches))


def _scope_for(*batches):
    @contextmanager
    def scope():
        yield _FakeSession(batches)

    return scope


class _FakeClient:
    def __init__(self, *, already_started_ids=()):
        self.calls = []
        self.already_started_ids = set(already_started_ids)

    async def start_workflow(self, workflow_run, *args, **kwargs):
        workflow_id = kwargs["id"]
        self.calls.append((workflow_run, args, kwargs))
        if workflow_id in self.already_started_ids:
            raise _AlreadyStarted()
        return SimpleNamespace(id=workflow_id)


class _AlreadyStarted(Exception):
    pass


def test_manual_resume_policy_allows_only_failed_temporal_execution() -> None:
    assert (
        orchestration_client._workflow_reuse_policy(allow_failed_reuse=False)
        == orchestration_client.WorkflowIDReusePolicy.REJECT_DUPLICATE
    )
    assert (
        orchestration_client._workflow_reuse_policy(allow_failed_reuse=True)
        == orchestration_client.WorkflowIDReusePolicy.ALLOW_DUPLICATE_FAILED_ONLY
    )


@pytest.mark.asyncio
async def test_intelligence_worker_reconciles_persisted_discovery_runs(
    monkeypatch,
) -> None:
    rows = [
        SimpleNamespace(id="discovery-1"),
        SimpleNamespace(id="discovery-2"),
    ]
    monkeypatch.setattr(
        intelligence_worker,
        "list_resumable_source_runs",
        lambda: rows,
    )
    monkeypatch.setattr(
        intelligence_worker,
        "WorkflowAlreadyStartedError",
        _AlreadyStarted,
    )
    client = _FakeClient(
        already_started_ids={"discovery-run-discovery-2"},
    )

    resumed, present = await intelligence_worker._resume_persisted_discovery_work(
        client,
    )

    assert resumed == 1
    assert present == 1
    assert [call[2]["id"] for call in client.calls] == [
        "discovery-run-discovery-1",
        "discovery-run-discovery-2",
    ]
    assert all(
        call[2]["task_queue"] == intelligence_worker.DISCOVERY_TASK_QUEUE
        for call in client.calls
    )
    assert all(
        call[2]["id_reuse_policy"]
        == intelligence_worker.WorkflowIDReusePolicy.ALLOW_DUPLICATE_FAILED_ONLY
        for call in client.calls
    )


@pytest.mark.asyncio
async def test_intelligence_worker_reconciles_nonterminal_command_goals(
    monkeypatch,
) -> None:
    goals = [
        SimpleNamespace(id="goal-1", status="running"),
        SimpleNamespace(id="goal-2", status="waiting_workflow"),
    ]
    monkeypatch.setattr(
        intelligence_worker,
        "session_scope",
        _scope_for(goals),
    )
    monkeypatch.setattr(
        intelligence_worker,
        "WorkflowAlreadyStartedError",
        _AlreadyStarted,
    )
    client = _FakeClient(already_started_ids={"command-goal-goal-2"})

    resumed, present = await intelligence_worker._resume_persisted_command_goals(
        client,
    )

    assert resumed == 1
    assert present == 1
    assert [call[2]["id"] for call in client.calls] == [
        "command-goal-goal-1",
        "command-goal-goal-2",
    ]
    assert [call[1][0] for call in client.calls] == ["goal-1", "goal-2"]
    assert all(
        call[2]["task_queue"] == intelligence_worker.INTELLIGENCE_TASK_QUEUE
        for call in client.calls
    )
    assert all(
        call[2]["id_reuse_policy"]
        == intelligence_worker.WorkflowIDReusePolicy.ALLOW_DUPLICATE_FAILED_ONLY
        for call in client.calls
    )


@pytest.mark.asyncio
async def test_standalone_longform_worker_reconciles_persisted_compilation(
    monkeypatch,
) -> None:
    compilation = SimpleNamespace(
        id="comp-standalone",
        workflow_id="compilation-standalone",
        status="voicing",
    )
    monkeypatch.setattr(
        longform_worker,
        "session_scope",
        _scope_for([compilation]),
    )
    monkeypatch.setattr(
        longform_worker,
        "WorkflowAlreadyStartedError",
        _AlreadyStarted,
    )
    client = _FakeClient()
    settings = SimpleNamespace(temporal_longform_task_queue="longform")

    resumed, present = await longform_worker._resume_persisted_longform_work(
        client,
        settings,
    )

    assert resumed == 1
    assert present == 0
    assert client.calls[0][2]["id"] == "compilation-standalone"
    assert client.calls[0][2]["args"] == ["comp-standalone", "voice"]
    assert (
        client.calls[0][2]["id_reuse_policy"]
        == longform_worker.WorkflowIDReusePolicy.ALLOW_DUPLICATE_FAILED_ONLY
    )


@pytest.mark.asyncio
async def test_production_worker_reconciles_persisted_execution_stages(
    monkeypatch,
) -> None:
    production = SimpleNamespace(
        id="prod-1",
        workflow_id="production-prod-1",
        status="scripted",
        stage="scripts_ready",
    )
    episode = SimpleNamespace(
        id="episode-1",
        workflow_id="episode-base",
        status="editorial_approved",
        stage="editorial_approved",
    )
    compilation = SimpleNamespace(
        id="comp-1",
        workflow_id="compilation-comp-1",
        status="rendering",
        stage="rendering",
    )
    preview = SimpleNamespace(
        id="preview-1",
        workflow_id="brand-preview-1",
        status="rendering",
    )
    monkeypatch.setattr(
        production_worker,
        "session_scope",
        _scope_for([production], [episode], [compilation], [preview]),
    )
    monkeypatch.setattr(
        production_worker,
        "WorkflowAlreadyStartedError",
        _AlreadyStarted,
    )
    client = _FakeClient(already_started_ids={"episode-base-editorial-render"})
    settings = SimpleNamespace(
        temporal_production_task_queue="production",
        temporal_longform_task_queue="longform",
    )

    resumed, present = await production_worker._resume_persisted_production_work(
        client,
        settings,
    )

    assert resumed == 3
    assert present == 1
    calls = {
        call[2]["id"]: call
        for call in client.calls
    }
    assert calls["production-prod-1"][2]["args"] == ["prod-1", "voice"]
    assert calls["episode-base-editorial-render"][2]["args"] == ["episode-1", "render"]
    assert calls["compilation-comp-1"][2]["args"] == ["comp-1", "render"]
    assert calls["brand-preview-1"][1][0] == "preview-1"
    assert calls["brand-preview-1"][2]["task_queue"] == "production"
    assert all(
        call[2]["id_reuse_policy"]
        == production_worker.WorkflowIDReusePolicy.ALLOW_DUPLICATE_FAILED_ONLY
        for call in client.calls
    )


@pytest.mark.asyncio
async def test_ingest_worker_reconciles_ingest_and_publication(
    monkeypatch,
) -> None:
    source = SimpleNamespace(
        id="source-1",
        workflow_id="ingest-source-1",
        status="registered",
    )
    publication = SimpleNamespace(
        id="publication-1",
        workflow_id="publish-publication-1",
        status="processing",
    )
    packaging = SimpleNamespace(
        id="packaging-1",
        workflow_id="packaging-workflow-1",
        status="running",
    )
    monkeypatch.setattr(
        worker,
        "session_scope",
        _scope_for([source], [publication], [packaging]),
    )
    monkeypatch.setattr(worker, "WorkflowAlreadyStartedError", _AlreadyStarted)
    client = _FakeClient(already_started_ids={"ingest-source-1"})
    settings = SimpleNamespace(
        temporal_task_queue="ingest",
        temporal_publishing_task_queue="publishing",
        youtube_processing_poll_seconds=20,
        youtube_processing_max_polls=10,
        analytics_offsets_hours=lambda: [24, 72],
    )

    resumed, present = await worker._resume_persisted_ingest_and_publication_work(
        client,
        settings,
    )

    assert resumed == 2
    assert present == 1
    calls = {call[2]["id"]: call for call in client.calls}
    assert calls["ingest-source-1"][1][0] == "source-1"
    assert calls["publish-publication-1"][2]["args"] == [
        "publication-1",
        20,
        10,
        [24, 72],
    ]
    assert calls["packaging-workflow-1"][1][0] == "packaging-1"
    assert calls["packaging-workflow-1"][2]["task_queue"] == "publishing"
    assert all(
        call[2]["id_reuse_policy"]
        == worker.WorkflowIDReusePolicy.ALLOW_DUPLICATE_FAILED_ONLY
        for call in client.calls
    )


@pytest.mark.asyncio
async def test_analysis_worker_reconciles_queued_and_running_analysis(
    monkeypatch,
) -> None:
    rows = [
        SimpleNamespace(id="analysis-1", workflow_id="analysis-workflow-1"),
        SimpleNamespace(id="analysis-2", workflow_id="analysis-workflow-2"),
    ]
    monkeypatch.setattr(
        analysis_worker,
        "session_scope",
        _scope_for(rows),
    )
    monkeypatch.setattr(
        analysis_worker,
        "WorkflowAlreadyStartedError",
        _AlreadyStarted,
    )
    client = _FakeClient(already_started_ids={"analysis-workflow-2"})
    settings = SimpleNamespace(
        temporal_analysis_task_queue="analysis",
        ai_enabled=True,
    )

    resumed, present = await analysis_worker._resume_persisted_analysis_work(
        client,
        settings,
    )

    assert resumed == 1
    assert present == 1
    assert [call[2]["id"] for call in client.calls] == [
        "analysis-workflow-1",
        "analysis-workflow-2",
    ]
    assert all(call[2]["args"][1] is True for call in client.calls)
    assert all(
        call[2]["id_reuse_policy"]
        == analysis_worker.WorkflowIDReusePolicy.ALLOW_DUPLICATE_FAILED_ONLY
        for call in client.calls
    )


@pytest.mark.asyncio
async def test_intelligence_worker_reconciles_persisted_automation_schedules(
    monkeypatch,
) -> None:
    schedules = [
        SimpleNamespace(
            id="schedule-1",
            schedule_kind="channel_intelligence",
            subject_id="channel-1",
            workflow_id="channel-intelligence-schedule-channel-1-g1",
            supersedes_workflow_id="channel-intelligence-schedule-channel-1",
            schedule_config={"interval_hours": 6},
        ),
        SimpleNamespace(
            id="schedule-2",
            schedule_kind="topic_watch",
            subject_id="watch-1",
            workflow_id="topic-watch-schedule-watch-1-g2",
            supersedes_workflow_id=None,
            schedule_config={"interval_minutes": 30, "top_n": 25},
        ),
    ]
    monkeypatch.setattr(
        intelligence_worker,
        "list_enabled_automation_schedules",
        lambda: schedules,
    )
    reconciled = []
    monkeypatch.setattr(
        intelligence_worker,
        "mark_schedule_reconciled",
        lambda schedule_id, workflow_id: reconciled.append(
            (schedule_id, workflow_id)
        ),
    )
    terminated = []

    async def fake_terminate(_client, workflow_id, *, reason):
        terminated.append((workflow_id, reason))
        return bool(workflow_id)

    monkeypatch.setattr(
        intelligence_worker,
        "terminate_workflow_if_running",
        fake_terminate,
    )
    monkeypatch.setattr(
        intelligence_worker,
        "WorkflowAlreadyStartedError",
        _AlreadyStarted,
    )
    client = _FakeClient(
        already_started_ids={"topic-watch-schedule-watch-1-g2"},
    )

    resumed, present = await intelligence_worker._resume_persisted_automation_schedules(
        client,
    )

    assert resumed == 1
    assert present == 1
    calls = {call[2]["id"]: call for call in client.calls}
    intelligence = calls["channel-intelligence-schedule-channel-1-g1"]
    assert intelligence[2]["args"] == ["channel-1", 6, 120]
    assert intelligence[2]["task_queue"] == intelligence_worker.INTELLIGENCE_TASK_QUEUE
    watch = calls["topic-watch-schedule-watch-1-g2"]
    assert watch[2]["args"] == ["watch-1", 30, 25, 0]
    assert watch[2]["task_queue"] == intelligence_worker.DISCOVERY_TASK_QUEUE
    assert terminated[0][0] == "channel-intelligence-schedule-channel-1"
    assert reconciled == [
        ("schedule-1", "channel-intelligence-schedule-channel-1-g1"),
        ("schedule-2", "topic-watch-schedule-watch-1-g2"),
    ]
