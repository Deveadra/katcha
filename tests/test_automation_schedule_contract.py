from __future__ import annotations

from sqlalchemy import CheckConstraint, UniqueConstraint

from katcha.api.trend_auto_activation import get_trend_activation_schedule
from katcha.automation_schedule_models import AutomationSchedule
from katcha.services.automation_schedules import register_channel_trend_activation_schedule
from katcha.orchestration.recovery_registry import (
    WORKFLOW_RECOVERY_CONTRACTS,
    RecoveryMode,
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
