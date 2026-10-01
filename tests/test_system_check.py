import importlib
from types import SimpleNamespace

import pytest

from katcha.config import Settings

checks = importlib.import_module("katcha.services.system_check")


@pytest.mark.asyncio
async def test_system_check_detects_missing_workers_without_claiming_live_ai(monkeypatch):
    monkeypatch.setattr(checks, "get_settings", lambda: Settings())
    monkeypatch.setattr(checks, "_database_check", lambda: {"research_last_checked_at": None})
    monkeypatch.setattr(checks, "subscription_connected", lambda settings: False)

    async def describe_workflow():
        return SimpleNamespace(status=SimpleNamespace(name="RUNNING"))

    async def temporal():
        return SimpleNamespace(
            workflow_service=SimpleNamespace(describe_task_queue=describe_queue),
            get_workflow_handle=lambda key: SimpleNamespace(describe=describe_workflow),
        )

    class HTTP:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def get(self, url):
            return SimpleNamespace(raise_for_status=lambda: None)

    monkeypatch.setattr(checks, "get_temporal_client", temporal)
    monkeypatch.setattr(checks.httpx, "AsyncClient", HTTP)
    # Select the actual configured queue, so a renamed queue remains covered.
    missing = Settings().temporal_task_queue

    async def describe_queue(request, **kwargs):
        return SimpleNamespace(pollers=[] if request.task_queue.name == missing else [1])

    result = await checks.system_check()
    by_key = {row["key"]: row for row in result["checks"]}
    assert by_key["database"]["status"] == "ok"
    assert by_key["ingestion"]["status"] == "unavailable"
    assert by_key["analysis"]["status"] == "ok"
    assert by_key["ai"]["status"] == "unavailable"
    assert result["live_inference_verified"] is False
