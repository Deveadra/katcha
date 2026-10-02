from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from enum import StrEnum

from sqlalchemy import select

from katcha.automation_schedule_models import AutomationSchedule
from katcha.db import session_scope


class AutomationScheduleKind(StrEnum):
    CHANNEL_INTELLIGENCE = "channel_intelligence"
    CHANNEL_TREND_ACTIVATION = "channel_trend_activation"
    TOPIC_WATCH = "topic_watch"


@dataclass(frozen=True, slots=True)
class ScheduleRegistration:
    schedule: AutomationSchedule
    supersedes_workflow_id: str | None
    changed: bool


def _register_schedule(
    *,
    schedule_kind: AutomationScheduleKind,
    subject_id: uuid.UUID,
    workflow_base_id: str,
    schedule_config: dict[str, object],
    replace_existing: bool = True,
) -> ScheduleRegistration:
    schedule_key = f"{schedule_kind.value}:{subject_id}"
    with session_scope() as session:
        row = session.scalar(
            select(AutomationSchedule)
            .where(AutomationSchedule.schedule_key == schedule_key)
            .with_for_update()
        )
        if row is None:
            row = AutomationSchedule(
                schedule_key=schedule_key,
                schedule_kind=schedule_kind.value,
                subject_id=subject_id,
                workflow_id=f"{workflow_base_id}-g1",
                supersedes_workflow_id=workflow_base_id,
                generation=1,
                enabled=True,
                schedule_config=dict(schedule_config),
            )
            session.add(row)
            session.flush()
            changed = True
        elif (
            not replace_existing
            or row.enabled and dict(row.schedule_config or {}) == schedule_config
        ):
            changed = False
        else:
            previous_workflow_id = row.workflow_id
            row.generation += 1
            row.workflow_id = f"{workflow_base_id}-g{row.generation}"
            row.supersedes_workflow_id = previous_workflow_id
            row.enabled = True
            row.schedule_config = dict(schedule_config)
            changed = True

        supersedes = row.supersedes_workflow_id
        session.flush()
        session.refresh(row)
        session.expunge(row)
        return ScheduleRegistration(
            schedule=row,
            supersedes_workflow_id=supersedes,
            changed=changed,
        )


def register_channel_intelligence_schedule(
    channel_profile_id: uuid.UUID,
    *,
    interval_hours: int,
    replace_existing: bool = True,
) -> ScheduleRegistration:
    if interval_hours < 1 or interval_hours > 168:
        raise ValueError("intelligence schedule interval must be between 1 and 168 hours")
    return _register_schedule(
        schedule_kind=AutomationScheduleKind.CHANNEL_INTELLIGENCE,
        subject_id=channel_profile_id,
        workflow_base_id=f"channel-intelligence-schedule-{channel_profile_id}",
        schedule_config={"interval_hours": interval_hours},
        replace_existing=replace_existing,
    )


def register_channel_trend_activation_schedule(
    channel_profile_id: uuid.UUID,
    *,
    interval_hours: int,
) -> ScheduleRegistration:
    if interval_hours < 1 or interval_hours > 168:
        raise ValueError("trend activation interval must be between 1 and 168 hours")
    return _register_schedule(
        schedule_kind=AutomationScheduleKind.CHANNEL_TREND_ACTIVATION,
        subject_id=channel_profile_id,
        workflow_base_id=f"trend-auto-activation-schedule-{channel_profile_id}",
        schedule_config={"interval_hours": interval_hours},
    )


def register_topic_watch_schedule(
    topic_watch_id: uuid.UUID,
    *,
    interval_minutes: int,
    top_n: int,
) -> ScheduleRegistration:
    if interval_minutes < 5 or interval_minutes > 7 * 24 * 60:
        raise ValueError("topic watch interval must be between 5 minutes and 7 days")
    if top_n < 1 or top_n > 250:
        raise ValueError("topic watch top_n must be between 1 and 250")
    return _register_schedule(
        schedule_kind=AutomationScheduleKind.TOPIC_WATCH,
        subject_id=topic_watch_id,
        workflow_base_id=f"topic-watch-schedule-{topic_watch_id}",
        schedule_config={
            "interval_minutes": interval_minutes,
            "top_n": top_n,
        },
    )


def list_enabled_automation_schedules() -> list[AutomationSchedule]:
    with session_scope() as session:
        rows = list(
            session.scalars(
                select(AutomationSchedule)
                .where(AutomationSchedule.enabled.is_(True))
                .order_by(
                    AutomationSchedule.schedule_kind,
                    AutomationSchedule.schedule_key,
                )
            )
        )
        for row in rows:
            session.expunge(row)
        return rows


@contextmanager
def locked_schedule_for_reconcile(
    schedule_id: uuid.UUID,
) -> Iterator[AutomationSchedule]:
    with session_scope() as session:
        row = session.scalar(
            select(AutomationSchedule)
            .where(AutomationSchedule.id == schedule_id)
            .with_for_update()
        )
        if row is None:
            raise RuntimeError(f"automation schedule disappeared: {schedule_id}")
        yield row


def mark_schedule_reconciled(schedule_id: uuid.UUID, workflow_id: str) -> None:
    with locked_schedule_for_reconcile(schedule_id) as row:
        if row.workflow_id != workflow_id:
            return
        row.supersedes_workflow_id = None
