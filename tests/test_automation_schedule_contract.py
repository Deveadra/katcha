from __future__ import annotations

from sqlalchemy import CheckConstraint, UniqueConstraint

from katcha.automation_schedule_models import AutomationSchedule
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
