"""Exercise the actual consolidated worker entry point without provider or DB work."""
import asyncio
from types import SimpleNamespace

import pytest

from katcha.orchestration import longform_worker, production_worker
from katcha.orchestration.editorial_activities import EDITORIAL_ACTIVITIES
from katcha.orchestration.editorial_workflows import EditorialProjectWorkflow
from katcha.orchestration.scene_index_dispatch import (
    fail_scene_index_attempt,
    measure_project_media,
)
from katcha.orchestration.scene_index_workflows import SceneIndexWorkflow


@pytest.mark.asyncio
@pytest.mark.parametrize("worker_failure", [False, True])
async def test_hosted_entry_point_serves_editorial_and_cleans_up_reconciliation(
    monkeypatch, worker_failure
):
    client = object()
    settings = SimpleNamespace(
        log_level="CRITICAL", temporal_host="fixture", temporal_namespace="fixture",
        temporal_production_task_queue="production", temporal_longform_task_queue="longform",
    )
    started = asyncio.Event()
    stopped = asyncio.Event()

    async def connect(*args, **kwargs):
        return client

    async def resume(*args):
        return 0, 0

    def worker_factory(worker_client, **kwargs):
        assert worker_client is client
        return SimpleNamespace(**kwargs)

    async def reconcile(worker_client):
        assert worker_client is client
        started.set()
        try:
            await asyncio.Future()
        finally:
            stopped.set()

    async def group(workers):
        await asyncio.wait_for(started.wait(), 1)
        by_queue = {worker.task_queue: worker for worker in workers}
        assert set(by_queue) == {"production", "longform"}
        hosted = by_queue["longform"]
        assert EditorialProjectWorkflow in hosted.workflows
        assert SceneIndexWorkflow in hosted.workflows
        assert longform_worker.LongformCompilationWorkflow in hosted.workflows
        assert set(EDITORIAL_ACTIVITIES).issubset(hosted.activities)
        assert measure_project_media in hosted.activities
        assert fail_scene_index_attempt in hosted.activities
        assert production_worker.ShortProductionWorkflow in by_queue["production"].workflows
        if worker_failure:
            raise RuntimeError("fixture worker failure")

    monkeypatch.setattr(production_worker, "get_settings", lambda: settings)
    monkeypatch.setattr(production_worker, "seed_builtin_brand_assets", lambda: [])
    monkeypatch.setattr(production_worker.Client, "connect", connect)
    monkeypatch.setattr(production_worker, "_resume_persisted_production_work", resume)
    monkeypatch.setattr(production_worker, "Worker", worker_factory)
    monkeypatch.setattr(longform_worker, "Worker", worker_factory)
    monkeypatch.setattr(production_worker, "editorial_reconciler", reconcile)
    monkeypatch.setattr(production_worker, "run_worker_group", group)
    if worker_failure:
        with pytest.raises(RuntimeError, match="fixture worker failure"):
            await production_worker.main()
    else:
        await production_worker.main()
    assert stopped.is_set()
