from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from katcha.orchestration import analysis_worker, production_worker, worker


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
    monkeypatch.setattr(
        production_worker,
        "session_scope",
        _scope_for([production], [episode], [compilation]),
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

    assert resumed == 2
    assert present == 1
    calls = {
        call[2]["id"]: call
        for call in client.calls
    }
    assert calls["production-prod-1"][1][0] == ["prod-1", "voice"]
    assert calls["episode-base-editorial-render"][1][0] == ["episode-1", "render"]
    assert calls["compilation-comp-1"][1][0] == ["comp-1", "render"]


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
    monkeypatch.setattr(
        worker,
        "session_scope",
        _scope_for([source], [publication]),
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

    assert resumed == 1
    assert present == 1
    calls = {call[2]["id"]: call for call in client.calls}
    assert calls["ingest-source-1"][1][0] == "source-1"
    assert calls["publish-publication-1"][1][0] == [
        "publication-1",
        20,
        10,
        [24, 72],
    ]


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
    assert all(call[1][0][1] is True for call in client.calls)
