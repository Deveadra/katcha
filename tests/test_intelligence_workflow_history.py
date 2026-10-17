"""Legacy command-sequence replay uses scrubbed payloads and no provider calls."""

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from temporalio.client import WorkflowHistory
from temporalio.worker import Replayer

from katcha.orchestration import intelligence_workflows as workflows


@pytest.mark.parametrize(
    "filename",
    [
        "intelligence-schedule-before-growth.json",
        "intelligence-schedule-with-growth-unmarked.json",
    ],
)
def test_scheduler_replays_both_unmarked_sequence_versions(filename):
    path = Path(__file__).parent / "fixtures" / filename
    history = WorkflowHistory.from_json("fixture-channel-intelligence", path.read_text())

    async def replay():
        await asyncio.wait_for(
            Replayer(workflows=[workflows.ChannelIntelligenceScheduleWorkflow]).replay_workflow(
                history
            ),
            timeout=15,
        )

    asyncio.run(replay())


@pytest.mark.parametrize(
    "start_time",
    [
        datetime(2026, 9, 25, tzinfo=UTC),
        datetime(2026, 10, 1, tzinfo=UTC),
    ],
)
def test_new_refresh_schedules_new_activities_once(monkeypatch, start_time):
    calls = []

    async def execute(name, *args, **kwargs):
        calls.append(name)
        return {"status": "ok"}

    monkeypatch.setattr(workflows.workflow, "patched", lambda marker: True)
    monkeypatch.setattr(
        workflows.workflow,
        "info",
        lambda: SimpleNamespace(start_time=start_time),
    )
    monkeypatch.setattr(workflows.workflow, "now", lambda: datetime(2026, 10, 17, tzinfo=UTC))
    monkeypatch.setattr(workflows.workflow, "execute_activity", execute)
    result = asyncio.run(workflows._run_refresh("fixture-channel", "fixture-run"))
    assert calls.count("refresh_channel_growth_activity") == 1
    assert calls.count("run_clip_lifecycle_maintenance_activity") == 1
    assert result["growth"]["status"] == "ok"
    assert result["clip_lifecycle"]["status"] == "ok"
