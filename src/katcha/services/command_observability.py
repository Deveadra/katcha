from __future__ import annotations

import logging
import math
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import select

from katcha.db import session_scope
from katcha.models import DomainEvent, UsageEvent

logger = logging.getLogger(__name__)


def _record_event(event_type: str, request_id: uuid.UUID, payload: dict[str, object]) -> None:
    try:
        with session_scope() as session:
            session.add(
                DomainEvent(
                    aggregate_type="command_request",
                    aggregate_id=str(request_id),
                    event_type=event_type,
                    payload=payload,
                )
            )
    except Exception as exc:
        logger.warning(
            "command observability write failed request_id=%s event_type=%s cause=%s",
            request_id,
            event_type,
            type(exc).__name__,
        )


def record_command_observation(
    *,
    channel_profile_id: uuid.UUID,
    request_id: uuid.UUID,
    thread_id: uuid.UUID,
    actor: str,
    intent: str,
    planning_source: str,
    planning_provider: str,
    planning_model: str,
    planning_confidence: float,
    narrator_provider: str,
    narrator_model: str,
    narrator_degraded_reason: str | None,
    latency_ms: int,
    evidence_count: int,
    action_count: int,
    resource_kinds: list[str],
) -> None:
    payload = {
        "channel_profile_id": str(channel_profile_id),
        "request_id": str(request_id),
        "thread_id": str(thread_id),
        "actor": actor,
        "intent": intent,
        "planning_source": planning_source,
        "planning_provider": planning_provider,
        "planning_model": planning_model,
        "planning_confidence": planning_confidence,
        "narrator_provider": narrator_provider,
        "narrator_model": narrator_model,
        "narrator_degraded": bool(narrator_degraded_reason),
        "narrator_degraded_reason": narrator_degraded_reason,
        "latency_ms": max(0, int(latency_ms)),
        "evidence_count": max(0, int(evidence_count)),
        "action_count": max(0, int(action_count)),
        "resource_context_count": len(resource_kinds),
        "resource_kinds": list(resource_kinds),
    }
    payload["outcome"] = "completed"
    _record_event("command_center.command_completed", request_id, payload)


def record_command_failure(
    *,
    channel_profile_id: uuid.UUID,
    request_id: uuid.UUID,
    actor: str,
    latency_ms: int,
    stage: str,
    status_code: int,
    error_type: str,
    resource_kinds: list[str],
) -> None:
    _record_event(
        "command_center.command_failed",
        request_id,
        {
            "channel_profile_id": str(channel_profile_id),
            "request_id": str(request_id),
            "thread_id": None,
            "actor": actor,
            "intent": None,
            "planning_source": None,
            "planning_provider": None,
            "planning_model": None,
            "planning_confidence": None,
            "narrator_provider": None,
            "narrator_model": None,
            "narrator_degraded": False,
            "narrator_degraded_reason": None,
            "latency_ms": max(0, int(latency_ms)),
            "evidence_count": 0,
            "action_count": 0,
            "resource_context_count": len(resource_kinds),
            "resource_kinds": list(resource_kinds),
            "outcome": "failed",
            "failure_stage": stage,
            "status_code": int(status_code),
            "error_type": error_type,
        },
    )


def _percentile(values: list[int], fraction: float) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, math.ceil(len(ordered) * fraction) - 1))
    return ordered[index]


def command_observability_summary(
    channel_profile_id: uuid.UUID,
    *,
    hours: int = 24,
) -> dict[str, object]:
    hours = max(1, min(24 * 30, int(hours)))
    since = datetime.now(UTC) - timedelta(hours=hours)
    channel_value = str(channel_profile_id)

    with session_scope() as session:
        command_events = list(
            session.scalars(
                select(DomainEvent)
                .where(
                    DomainEvent.event_type.in_(
                        (
                            "command_center.command_completed",
                            "command_center.command_failed",
                        )
                    ),
                    DomainEvent.created_at >= since,
                    DomainEvent.payload["channel_profile_id"].as_string()
                    == channel_value,
                )
                .order_by(DomainEvent.created_at.desc())
                .limit(2000)
            )
        )
        request_ids = {row.aggregate_id for row in command_events}

        usage_rows: list[UsageEvent] = []
        if request_ids:
            usage_rows = list(
                session.scalars(
                    select(UsageEvent).where(
                        UsageEvent.reference_id.in_(request_ids),
                        UsageEvent.created_at >= since,
                        UsageEvent.reference_type.in_(
                            ("command_center", "command_planner")
                        ),
                    )
                )
            )

        lifecycle_events = list(
            session.scalars(
                select(DomainEvent)
                .where(
                    DomainEvent.event_type.in_(
                        (
                            "command_center.proposal_created",
                            "command_center.action_executed",
                            "command_center.action_failed",
                        )
                    ),
                    DomainEvent.created_at >= since,
                    DomainEvent.payload["channel_profile_id"].as_string()
                    == channel_value,
                )
                .order_by(DomainEvent.created_at.desc())
                .limit(4000)
            )
        )
    latencies = [
        int((row.payload or {}).get("latency_ms") or 0)
        for row in command_events
    ]
    completed = sum(
        row.event_type == "command_center.command_completed"
        for row in command_events
    )
    failed = sum(
        row.event_type == "command_center.command_failed"
        for row in command_events
    )
    degraded = sum(
        bool((row.payload or {}).get("narrator_degraded"))
        for row in command_events
    )
    ai_planned = sum(
        str((row.payload or {}).get("planning_source") or "").startswith("ai")
        for row in command_events
    )
    typed_context = sum(
        int((row.payload or {}).get("resource_context_count") or 0) > 0
        for row in command_events
    )

    total_cost = sum(
        (row.cost_usd or Decimal("0"))
        for row in usage_rows
    )
    input_units = sum(int(row.input_units or 0) for row in usage_rows)
    output_units = sum(int(row.output_units or 0) for row in usage_rows)
    action_counts = {
        "proposed": 0,
        "executed": 0,
        "failed": 0,
    }
    mapping = {
        "command_center.proposal_created": "proposed",
        "command_center.action_executed": "executed",
        "command_center.action_failed": "failed",
    }
    for row in lifecycle_events:
        action_counts[mapping[row.event_type]] += 1

    recent = []
    for row in command_events[:20]:
        payload = dict(row.payload or {})
        recent.append(
            {
                "request_id": row.aggregate_id,
                "thread_id": payload.get("thread_id"),
                "intent": payload.get("intent"),
                "outcome": payload.get("outcome") or (
                    "failed"
                    if row.event_type == "command_center.command_failed"
                    else "completed"
                ),
                "failure_stage": payload.get("failure_stage"),
                "status_code": payload.get("status_code"),
                "latency_ms": int(payload.get("latency_ms") or 0),
                "planning_source": payload.get("planning_source"),
                "narrator": (
                    f"{payload.get('narrator_provider')}/"
                    f"{payload.get('narrator_model')}"
                ),
                "degraded": bool(payload.get("narrator_degraded")),
                "resource_kinds": list(payload.get("resource_kinds") or []),
                "created_at": row.created_at.isoformat(),
            }
        )

    request_count = len(command_events)
    return {
        "channel_profile_id": str(channel_profile_id),
        "window_hours": hours,
        "request_count": request_count,
        "completed_request_count": completed,
        "failed_request_count": failed,
        "average_latency_ms": (
            round(sum(latencies) / len(latencies)) if latencies else 0
        ),
        "p95_latency_ms": _percentile(latencies, 0.95),
        "degraded_answer_count": degraded,
        "ai_planned_count": ai_planned,
        "typed_context_request_count": typed_context,
        "input_units": input_units,
        "output_units": output_units,
        "estimated_cost_usd": float(total_cost),
        "actions": action_counts,
        "recent": recent,
    }
