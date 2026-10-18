from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import CheckConstraint, UniqueConstraint, select

from katcha.api.trend_auto_activation import get_trend_activation_schedule
from katcha.automation_schedule_models import AutomationSchedule
from katcha.db import session_scope
from katcha.trend_activation_models import TrendActivationRun
from katcha.orchestration.recovery_registry import (
    WORKFLOW_RECOVERY_CONTRACTS,
    RecoveryMode,
)
from katcha.services.automation_schedules import (
    mark_schedule_reconciled,
    register_channel_trend_activation_schedule,
)


def _unique_columns() -> set[tuple[str, ...]]:
    return {
        tuple(column.name for column in constraint.columns)
        for constraint in AutomationSchedule.__table__.constraints
        if isinstance(constraint, UniqueConstraint)
    }


def test_automation_schedule_model_has_single_current_identity() -> None:
    unique = _unique_columns()
    assert ("schedule_key",) in unique
    assert ("workflow_id",) in unique
    checks = {
        constraint.name
        for constraint in AutomationSchedule.__table__.constraints
        if isinstance(constraint, CheckConstraint)
    }
    assert "ck_automation_schedule_generation_positive" in checks


def test_all_recurring_schedules_are_persisted_reconcile_workflows() -> None:
    for workflow in (
        "TopicWatchScheduleWorkflow",
        "ChannelIntelligenceScheduleWorkflow",
        "ChannelTrendActivationScheduleWorkflow",
    ):
        assert (
            WORKFLOW_RECOVERY_CONTRACTS[workflow].mode
            == RecoveryMode.PERSISTED_RECONCILE
        )


def test_trend_schedule_status_distinguishes_registration_from_worker_health(
    fixture_work,
) -> None:
    _, channel, other, _ = fixture_work
    absent = get_trend_activation_schedule(channel)
    assert not absent.registered
    assert not absent.enabled
    assert absent.interval_hours is None
    assert absent.runtime_verified is False

    registration = register_channel_trend_activation_schedule(
        channel, interval_hours=6
    )
    present = get_trend_activation_schedule(channel)
    assert present.registered
    assert present.enabled
    assert present.interval_hours == 6
    assert present.workflow_id == registration.schedule.workflow_id
    assert present.generation == 1
    assert present.reconcile_pending
    assert present.runtime_verified is False

    # Another channel does not inherit a schedule just because it shares a
    # configured operator or a running intelligence worker.
    unrelated = get_trend_activation_schedule(other)
    assert not unrelated.registered


def test_trend_schedule_status_reports_real_scheduled_receipts_not_manual_scans(
    fixture_work,
) -> None:
    _, channel, other, _ = fixture_work
    registered = register_channel_trend_activation_schedule(channel, interval_hours=3)
    mark_schedule_reconciled(
        registered.schedule.id, registered.schedule.workflow_id
    )
    now = datetime.now(UTC)
    with session_scope() as session:
        scheduled = TrendActivationRun(
            channel_profile_id=channel,
            run_key="scheduled-20261018T100000Z",
            status="completed",
            started_at=now - timedelta(minutes=40),
            completed_at=now - timedelta(minutes=38),
            run_metadata={},
        )
        manual = TrendActivationRun(
            channel_profile_id=channel,
            run_key="manual-newer-than-scheduled",
            status="completed",
            started_at=now - timedelta(minutes=2),
            completed_at=now - timedelta(minutes=1),
            run_metadata={},
        )
        unrelated = TrendActivationRun(
            channel_profile_id=other,
            run_key="scheduled-20261018T110000Z",
            status="completed",
            started_at=now - timedelta(minutes=1),
            completed_at=now,
            run_metadata={},
        )
        session.add_all([scheduled, manual, unrelated])
        session.flush()
        scheduled_id = scheduled.id

    current = get_trend_activation_schedule(channel)
    assert current.runtime_verified is False
    assert current.scan_health == "recent_scan"
    assert current.last_scheduled_scan_run_id == scheduled_id
    assert current.last_scheduled_scan_status == "completed"
    assert current.scan_overdue is False
    assert get_trend_activation_schedule(other).last_scheduled_scan_run_id is None


def test_trend_schedule_status_detects_missing_stale_and_stalled_receipts(
    fixture_work,
) -> None:
    _, channel, _, _ = fixture_work
    registered = register_channel_trend_activation_schedule(channel, interval_hours=1)
    mark_schedule_reconciled(
        registered.schedule.id, registered.schedule.workflow_id
    )
    assert get_trend_activation_schedule(channel).scan_health == "awaiting_first_scan"

    now = datetime.now(UTC)
    with session_scope() as session:
        scan = TrendActivationRun(
            channel_profile_id=channel,
            run_key="scheduled-20261018T000000Z",
            status="completed",
            started_at=now - timedelta(hours=4),
            completed_at=now - timedelta(hours=4) + timedelta(minutes=1),
            run_metadata={},
        )
        session.add(scan)
    overdue = get_trend_activation_schedule(channel)
    assert overdue.scan_health == "scan_overdue"
    assert overdue.scan_overdue is True

    with session_scope() as session:
        scan = session.scalar(
            select(TrendActivationRun).where(
                TrendActivationRun.channel_profile_id == channel,
                TrendActivationRun.run_key == "scheduled-20261018T000000Z",
            )
        )
        assert scan is not None
        scan.status = "running"
        scan.started_at = now - timedelta(hours=1)
        scan.completed_at = None
    stalled = get_trend_activation_schedule(channel)
    assert stalled.scan_health == "scan_stalled"
    assert stalled.scan_overdue is False
    assert stalled.runtime_verified is False

    with session_scope() as session:
        scan = session.scalar(
            select(TrendActivationRun).where(
                TrendActivationRun.channel_profile_id == channel,
                TrendActivationRun.run_key == "scheduled-20261018T000000Z",
            )
        )
        assert scan is not None
        scan.status = "disabled"
        scan.started_at = now - timedelta(minutes=5)
        scan.completed_at = now - timedelta(minutes=4)
    paused = get_trend_activation_schedule(channel)
    assert paused.scan_health == "scan_policy_disabled"
    assert paused.scan_overdue is False
