from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from temporalio import workflow

from katcha.db import session_scope
from katcha.orchestration import intelligence_activities, intelligence_workflows, trend_activities
from katcha.trend_activation_models import TrendActivationRun
from katcha.trends.runtime import TREND_TASK_QUEUE


@pytest.mark.asyncio
async def test_scheduled_cycle_scores_before_activation_on_trend_queue(monkeypatch):
    calls = []
    monkeypatch.setattr(
        workflow, "patched",
        lambda marker: marker == "scheduled-trend-refresh-before-activation-v1",
    )

    async def execute(name, **kwargs):
        calls.append((name, kwargs))
        if name == "refresh_scheduled_channel_trends_activity":
            return {
                "status": "completed", "topics_scored": 4,
                "qualified_opportunities": 2, "evidence_packets_built": 2,
                "source_token": "never-persist-raw-provider-data",
            }
        assert name == "run_channel_trend_activation_activity"
        return {"status": "completed"}

    monkeypatch.setattr(workflow, "execute_activity", execute)
    result = await intelligence_workflows._run_scheduled_trend_activation_cycle(
        "channel-1", "scheduled-20261018T120000Z"
    )
    assert result == {"status": "completed"}
    assert [name for name, _ in calls] == [
        "refresh_scheduled_channel_trends_activity",
        "run_channel_trend_activation_activity",
    ]
    assert calls[0][1]["task_queue"] == TREND_TASK_QUEUE
    assert calls[0][1]["args"] == ["channel-1", "scheduled-20261018T120000Z"]
    assert calls[0][1]["retry_policy"].maximum_attempts == 2
    assert calls[1][1]["args"] == [
        "channel-1", "scheduled-20261018T120000Z",
        {"status": "completed", "topics_scored": 4,
         "qualified_opportunities": 2, "evidence_packets_built": 2},
    ]


@pytest.mark.asyncio
async def test_legacy_schedule_history_preserves_activity_event_sequence(monkeypatch):
    monkeypatch.setattr(workflow, "patched", lambda _marker: False)
    recorded = []

    async def execute(name, **kwargs):
        recorded.append((name, kwargs.get("args")))
        return {"status": "completed"}

    monkeypatch.setattr(workflow, "execute_activity", execute)
    await intelligence_workflows._run_scheduled_trend_activation_cycle(
        "channel-1", "scheduled-legacy"
    )
    assert recorded == [
        ("run_channel_trend_activation_activity", ["channel-1", "scheduled-legacy"])
    ]


@pytest.mark.asyncio
async def test_refresh_failure_is_recorded_but_prior_eligible_activation_still_runs(
    monkeypatch,
):
    monkeypatch.setattr(workflow, "patched", lambda _marker: True)
    calls = []

    async def execute(name, **kwargs):
        calls.append((name, kwargs.get("args")))
        if name == "refresh_scheduled_channel_trends_activity":
            raise RuntimeError("provider error must not leak")
        return {"status": "completed"}

    monkeypatch.setattr(workflow, "execute_activity", execute)
    await intelligence_workflows._run_scheduled_trend_activation_cycle(
        "channel-1", "scheduled-20261018T130000Z"
    )
    assert calls[-1] == (
        "run_channel_trend_activation_activity",
        ["channel-1", "scheduled-20261018T130000Z", {"status": "refresh_failed"}],
    )


def test_scheduled_refresh_respects_disabled_channel_planning(monkeypatch):
    monkeypatch.setattr(
        trend_activities, "active_activation_policy", lambda _id: SimpleNamespace(enabled=False)
    )

    def do_not_refresh(*_args):
        raise AssertionError("disabled channel must not score trends")

    monkeypatch.setattr(trend_activities, "refresh_channel_trends_activity", do_not_refresh)
    result = trend_activities.refresh_scheduled_channel_trends_activity(
        str(uuid4()), "scheduled-disabled"
    )
    assert result == {
        "status": "planning_disabled", "topics_scored": 0,
        "qualified_opportunities": 0, "evidence_packets_built": 0,
    }


def test_scheduled_refresh_uses_existing_channel_scoring_activity(monkeypatch):
    channel = uuid4()
    calls = []
    monkeypatch.setattr(
        trend_activities, "active_activation_policy",
        lambda channel_id: (
            calls.append(("policy", channel_id)) or SimpleNamespace(enabled=True)
        ),
    )

    def refresh(channel_id, run_key):
        calls.append(("refresh", channel_id, run_key))
        return {"status": "completed", "topics_scored": 2}

    monkeypatch.setattr(trend_activities, "refresh_channel_trends_activity", refresh)
    result = trend_activities.refresh_scheduled_channel_trends_activity(
        str(channel), "scheduled-test"
    )
    assert result["topics_scored"] == 2
    assert calls == [("policy", channel), ("refresh", str(channel), "scheduled-test")]


@pytest.mark.asyncio
async def test_scheduled_activation_persists_only_sanitized_receipt_for_its_channel(
    fixture_work, monkeypatch,
):
    _, channel, other, _ = fixture_work
    with session_scope() as session:
        row = TrendActivationRun(
            channel_profile_id=channel, run_key="scheduled-20261018T120000Z",
            status="completed", run_metadata={"existing_marker": "retain"},
            started_at=datetime.now(UTC),
        )
        other_row = TrendActivationRun(
            channel_profile_id=other, run_key="scheduled-20261018T120000Z",
            status="completed", run_metadata={"other_marker": "safe"},
            started_at=datetime.now(UTC),
        )
        session.add_all([row, other_row])
        session.flush()
        run_id, other_id = row.id, other_row.id

    monkeypatch.setattr(
        intelligence_activities, "run_autonomous_trend_activation",
        lambda *_args, **_kw: SimpleNamespace(
            run_id=run_id, as_dict=lambda: {"run_id": str(run_id)}
        ),
    )
    monkeypatch.setattr(
        intelligence_activities, "dispatch_planned_trend_editorial",
        AsyncMock(return_value={"status": "blocked", "reason": "no_budget"}),
    )
    result = await intelligence_activities.run_channel_trend_activation_activity(
        str(channel), "scheduled-20261018T120000Z",
        {"status": "completed", "topics_scored": 3,
         "qualified_opportunities": 1, "token": "secret-value",
         "evidence_packets_built": 2, "invalid_count": True},
    )
    assert result["editorial_dispatch"]["status"] == "blocked"
    with session_scope() as session:
        own = session.get(TrendActivationRun, run_id)
        foreign = session.get(TrendActivationRun, other_id)
        assert own.run_metadata["existing_marker"] == "retain"
        assert own.run_metadata["trend_refresh"] == {
            "status": "completed", "topics_scored": 3,
            "qualified_opportunities": 1, "evidence_packets_built": 2,
        }
        assert own.run_metadata["editorial_dispatch"]["reason"] == "no_budget"
        assert foreign.run_metadata == {"other_marker": "safe"}


@pytest.mark.asyncio
async def test_manual_activation_does_not_gain_scheduled_refresh_metadata(
    fixture_work, monkeypatch,
):
    _, channel, _, _ = fixture_work
    with session_scope() as session:
        row = TrendActivationRun(
            channel_profile_id=channel, run_key="manual-run",
            status="completed", run_metadata={},
        )
        session.add(row)
        session.flush()
        run_id = row.id
    monkeypatch.setattr(
        intelligence_activities, "run_autonomous_trend_activation",
        lambda *_args, **_kw: SimpleNamespace(
            run_id=run_id, as_dict=lambda: {"run_id": str(run_id)}
        ),
    )
    monkeypatch.setattr(
        intelligence_activities, "dispatch_planned_trend_editorial",
        AsyncMock(return_value={"status": "waiting", "started": []}),
    )
    await intelligence_activities.run_channel_trend_activation_activity(
        str(channel), "manual-run"
    )
    with session_scope() as session:
        saved = session.get(TrendActivationRun, run_id)
        assert "trend_refresh" not in saved.run_metadata
