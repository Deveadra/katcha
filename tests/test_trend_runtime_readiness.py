from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from katcha.api import trend_auto_activation
from katcha.orchestration import trend_runtime_readiness as readiness
from katcha.services.automation_schedules import (
    mark_schedule_reconciled,
    register_channel_trend_activation_schedule,
)


def _poller(last: datetime):
    return SimpleNamespace(
        last_access_time=SimpleNamespace(
            ToDatetime=lambda tzinfo=None: last,
        )
    )


def _client(*, workflow_running=True, missing=None, failing=None):
    missing = set(missing or ())
    failing = set(failing or ())
    calls = []

    async def describe_queue(request, **kwargs):
        queue = (request.task_queue.name, int(request.task_queue_type))
        calls.append(queue)
        if queue in failing:
            raise RuntimeError("test injected poller failure")
        return SimpleNamespace(
            pollers=[] if queue in missing else [_poller(datetime.now(UTC))]
        )

    async def describe_workflow():
        if isinstance(workflow_running, Exception):
            raise workflow_running
        return SimpleNamespace(
            status=(
                readiness.WorkflowExecutionStatus.RUNNING
                if workflow_running
                else readiness.WorkflowExecutionStatus.COMPLETED
            )
        )

    handle = SimpleNamespace(describe=AsyncMock(side_effect=describe_workflow))
    client = SimpleNamespace(
        workflow_service=SimpleNamespace(describe_task_queue=describe_queue),
        get_workflow_handle=lambda workflow_id: handle,
    )
    return client, calls


@pytest.mark.asyncio
async def test_readiness_requires_active_schedule_and_both_worker_queues(monkeypatch):
    client, calls = _client()
    monkeypatch.setattr(
        readiness, "get_temporal_client", AsyncMock(return_value=client)
    )
    monkeypatch.setattr(
        readiness, "get_settings",
        lambda: SimpleNamespace(
            temporal_namespace="default",
            temporal_production_task_queue="katcha-production",
        ),
    )
    ready = await readiness.inspect_trend_schedule_runtime("trend-schedule-g1")
    assert ready["status"] == "queues_recently_polled"
    assert ready["runtime_verified"] is True
    assert ready["editorial_queue_ready"] is True
    assert len(calls) == 4
    assert len(set(calls)) == 4
    assert ready["checked_at"].tzinfo is not None


@pytest.mark.asyncio
async def test_missing_production_pollers_do_not_hide_intelligence_readiness(monkeypatch):
    queue = ("katcha-production", int(readiness.TaskQueueType.TASK_QUEUE_TYPE_ACTIVITY))
    client, _ = _client(missing={queue})
    monkeypatch.setattr(readiness, "get_temporal_client", AsyncMock(return_value=client))
    monkeypatch.setattr(
        readiness,
        "get_settings",
        lambda: SimpleNamespace(
            temporal_namespace="default",
            temporal_production_task_queue="katcha-production",
        ),
    )
    summary = await readiness.inspect_trend_schedule_runtime("scheduled-g1")
    assert summary["status"] == "editorial_worker_not_observed"
    assert summary["runtime_verified"] is True
    assert summary["editorial_queue_ready"] is False


@pytest.mark.asyncio
async def test_missing_intelligence_pollers_blocks_schedule_runtime_verification(monkeypatch):
    queue = (
        readiness.INTELLIGENCE_TASK_QUEUE,
        int(readiness.TaskQueueType.TASK_QUEUE_TYPE_WORKFLOW),
    )
    client, _ = _client(missing={queue})
    monkeypatch.setattr(readiness, "get_temporal_client", AsyncMock(return_value=client))
    monkeypatch.setattr(
        readiness,
        "get_settings",
        lambda: SimpleNamespace(
            temporal_namespace="default",
            temporal_production_task_queue="katcha-production",
        ),
    )
    result = await readiness.inspect_trend_schedule_runtime("scheduled-g1")
    assert result["status"] == "intelligence_worker_not_observed"
    assert result["runtime_verified"] is False


@pytest.mark.asyncio
async def test_rpc_errors_and_missing_workflow_fail_closed(monkeypatch):
    client, _ = _client(workflow_running=RuntimeError("injected"))
    monkeypatch.setattr(readiness, "get_temporal_client", AsyncMock(return_value=client))
    monkeypatch.setattr(
        readiness,
        "get_settings",
        lambda: SimpleNamespace(
            temporal_namespace="default",
            temporal_production_task_queue="katcha-production",
        ),
    )
    failure = await readiness.inspect_trend_schedule_runtime("scheduled-g1")
    assert failure["status"] == "workflow_unavailable"
    assert failure["runtime_verified"] is False
    assert "injected" not in str(failure)

    monkeypatch.setattr(
        readiness, "get_temporal_client", AsyncMock(side_effect=RuntimeError("secret"))
    )
    failure = await readiness.inspect_trend_schedule_runtime("scheduled-g1")
    assert failure["status"] == "unavailable"
    assert failure["reason"] == "temporal_connection_unavailable"
    assert "secret" not in str(failure)


def test_old_or_future_pollers_do_not_verify_runtime():
    now = datetime.now(UTC)
    assert readiness._recent_poller_present([_poller(now)], now=now)
    assert readiness._recent_poller_present(
        [_poller(now + timedelta(milliseconds=1))], now=now
    )
    assert not readiness._recent_poller_present(
        [_poller(now - timedelta(minutes=6))], now=now
    )
    assert not readiness._recent_poller_present(
        [_poller(now + timedelta(minutes=6))], now=now
    )
    assert not readiness._recent_poller_present([], now=now)


@pytest.mark.asyncio
async def test_api_probes_only_registered_enabled_channel(monkeypatch, fixture_work):
    _, channel, other, _ = fixture_work
    probe = AsyncMock(
        return_value={
            "checked_at": datetime.now(UTC),
            "status": "queues_recently_polled",
            "schedule_running": True,
            "runtime_verified": True,
            "editorial_queue_ready": True,
            "reason": None,
        }
    )
    monkeypatch.setattr(trend_auto_activation, "inspect_trend_schedule_runtime", probe)
    absent = await trend_auto_activation.get_trend_activation_runtime(channel)
    assert absent.status == "not_registered"
    probe.assert_not_awaited()

    registration = register_channel_trend_activation_schedule(
        channel, interval_hours=3
    )
    mark_schedule_reconciled(
        registration.schedule.id, registration.schedule.workflow_id
    )
    present = await trend_auto_activation.get_trend_activation_runtime(channel)
    assert present.channel_profile_id == channel
    assert present.runtime_verified
    probe.assert_awaited_once_with(registration.schedule.workflow_id)

    different = await trend_auto_activation.get_trend_activation_runtime(other)
    assert different.status == "not_registered"
    assert different.channel_profile_id == other
    assert probe.await_count == 1
