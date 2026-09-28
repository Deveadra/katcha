from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import desc, select

from katcha.db import session_scope
from katcha.intelligence_models import ChannelIntelligenceRun
from katcha.models import DomainEvent
from katcha.services.channel_profiles import ensure_active_profile

_SUMMARY_KEYS = {
    "status",
    "reason",
    "ranking_snapshot_id",
    "ranking_version",
    "sample_count",
    "confidence",
    "economics_snapshot_id",
    "recommendation_count",
    "snapshot_id",
    "version",
    "comparison_status",
    "recommendation_status",
    "performance_snapshot_id",
    "decision_count",
    "planned_count",
    "published_count",
    "contribution_margin_usd",
    "created_observations",
    "started",
    "advanced",
    "blocked",
}


def _now() -> datetime:
    return datetime.now(UTC)


def _validate_identity(run_key: str, workflow_id: str) -> tuple[str, str]:
    key = run_key.strip()
    workflow = workflow_id.strip()
    if not key or len(key) > 128:
        raise ValueError("intelligence run_key must contain 1-128 characters")
    if not workflow or len(workflow) > 255:
        raise ValueError("intelligence workflow_id must contain 1-255 characters")
    return key, workflow


def _event(
    run: ChannelIntelligenceRun,
    event_type: str,
    *,
    extra: dict[str, object] | None = None,
) -> DomainEvent:
    payload: dict[str, object] = {
        "intelligence_run_id": str(run.id),
        "channel_profile_id": str(run.channel_profile_id),
        "run_key": run.run_key,
        "workflow_id": run.workflow_id,
        "status": run.status,
        "stage": run.stage,
    }
    if extra:
        payload.update(extra)
    return DomainEvent(
        aggregate_type="channel_intelligence_run",
        aggregate_id=str(run.id),
        event_type=event_type,
        payload=payload,
    )


def _run_for_update(
    session: object,
    channel_profile_id: uuid.UUID,
    run_key: str,
) -> ChannelIntelligenceRun | None:
    return session.scalar(
        select(ChannelIntelligenceRun)
        .where(
            ChannelIntelligenceRun.channel_profile_id == channel_profile_id,
            ChannelIntelligenceRun.run_key == run_key,
        )
        .with_for_update()
    )


def _assert_workflow(run: ChannelIntelligenceRun, workflow_id: str) -> None:
    if run.workflow_id != workflow_id:
        raise ValueError(
            "intelligence run key is already bound to a different workflow"
        )


def _summary_value(value: object) -> object:
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, list):
        return {
            "count": len(value),
            "sample": [
                item
                for item in value[:3]
                if isinstance(item, (str, int, float, bool))
            ],
        }
    return str(value)[:240]


def _summarize_result(result: dict[str, object]) -> dict[str, object]:
    summary: dict[str, object] = {}
    for section, value in result.items():
        if section in {"channel_profile_id", "run_key"}:
            continue
        if isinstance(value, dict):
            selected = {
                str(key): _summary_value(item)
                for key, item in value.items()
                if str(key) in _SUMMARY_KEYS
            }
            if selected:
                summary[str(section)] = selected
        elif section in _SUMMARY_KEYS:
            summary[str(section)] = _summary_value(value)
    return summary


def start_channel_intelligence_run(
    channel_profile_id: uuid.UUID,
    *,
    run_key: str,
    workflow_id: str,
) -> ChannelIntelligenceRun:
    key, workflow = _validate_identity(run_key, workflow_id)
    now = _now()
    with session_scope() as session:
        ensure_active_profile(session, channel_profile_id)
        run = _run_for_update(session, channel_profile_id, key)
        emit_started = False
        if run is None:
            run = ChannelIntelligenceRun(
                channel_profile_id=channel_profile_id,
                run_key=key,
                workflow_id=workflow,
                status="running",
                stage="running",
                error=None,
                result_summary={},
                started_at=now,
            )
            session.add(run)
            session.flush()
            emit_started = True
        else:
            _assert_workflow(run, workflow)
            if run.status == "completed":
                session.expunge(run)
                return run
            if run.status != "running":
                run.status = "running"
                run.stage = "running"
                run.error = None
                run.completed_at = None
                emit_started = True

        if emit_started:
            session.add(_event(run, "channel_intelligence.run_started"))
        session.flush()
        session.refresh(run)
        session.expunge(run)
        return run


def complete_channel_intelligence_run(
    channel_profile_id: uuid.UUID,
    *,
    run_key: str,
    workflow_id: str,
    result: dict[str, object],
) -> ChannelIntelligenceRun:
    key, workflow = _validate_identity(run_key, workflow_id)
    now = _now()
    with session_scope() as session:
        ensure_active_profile(session, channel_profile_id)
        run = _run_for_update(session, channel_profile_id, key)
        if run is None:
            run = ChannelIntelligenceRun(
                channel_profile_id=channel_profile_id,
                run_key=key,
                workflow_id=workflow,
                status="running",
                stage="running",
                result_summary={},
                started_at=now,
            )
            session.add(run)
            session.flush()
            session.add(_event(run, "channel_intelligence.run_started"))
        else:
            _assert_workflow(run, workflow)
        if run.status == "completed":
            session.expunge(run)
            return run

        summary = _summarize_result(result)
        run.status = "completed"
        run.stage = "completed"
        run.error = None
        run.result_summary = summary
        run.completed_at = now
        session.add(
            _event(
                run,
                "channel_intelligence.run_completed",
                extra={"result_summary": summary},
            )
        )
        session.flush()
        session.refresh(run)
        session.expunge(run)
        return run


def fail_channel_intelligence_run(
    channel_profile_id: uuid.UUID,
    *,
    run_key: str,
    workflow_id: str,
    error: str,
) -> ChannelIntelligenceRun:
    key, workflow = _validate_identity(run_key, workflow_id)
    now = _now()
    message = error.strip() or "intelligence refresh failed"
    with session_scope() as session:
        ensure_active_profile(session, channel_profile_id)
        run = _run_for_update(session, channel_profile_id, key)
        if run is None:
            run = ChannelIntelligenceRun(
                channel_profile_id=channel_profile_id,
                run_key=key,
                workflow_id=workflow,
                status="running",
                stage="running",
                result_summary={},
                started_at=now,
            )
            session.add(run)
            session.flush()
            session.add(_event(run, "channel_intelligence.run_started"))
        else:
            _assert_workflow(run, workflow)
        if run.status == "completed":
            session.expunge(run)
            return run
        bounded_error = message[:4000]
        if run.status == "failed" and run.error == bounded_error:
            session.expunge(run)
            return run

        run.status = "failed"
        run.stage = "failed"
        run.error = bounded_error
        run.completed_at = now
        session.add(
            _event(
                run,
                "channel_intelligence.run_failed",
                extra={"error": run.error[:1000]},
            )
        )
        session.flush()
        session.refresh(run)
        session.expunge(run)
        return run


def get_channel_intelligence_run(
    channel_profile_id: uuid.UUID,
    run_key: str,
) -> ChannelIntelligenceRun:
    key = run_key.strip()
    if not key or len(key) > 128:
        raise ValueError("intelligence run_key must contain 1-128 characters")
    with session_scope() as session:
        ensure_active_profile(session, channel_profile_id)
        run = session.scalar(
            select(ChannelIntelligenceRun).where(
                ChannelIntelligenceRun.channel_profile_id == channel_profile_id,
                ChannelIntelligenceRun.run_key == key,
            )
        )
        if run is None:
            raise ValueError(f"channel intelligence run not found: {key}")
        session.expunge(run)
        return run


def list_channel_intelligence_runs(
    channel_profile_id: uuid.UUID,
    *,
    limit: int = 20,
) -> list[ChannelIntelligenceRun]:
    if limit < 1 or limit > 100:
        raise ValueError("intelligence run limit must be between 1 and 100")
    with session_scope() as session:
        ensure_active_profile(session, channel_profile_id)
        rows = list(
            session.scalars(
                select(ChannelIntelligenceRun)
                .where(
                    ChannelIntelligenceRun.channel_profile_id == channel_profile_id
                )
                .order_by(
                    desc(ChannelIntelligenceRun.started_at),
                    desc(ChannelIntelligenceRun.created_at),
                )
                .limit(limit)
            )
        )
        for row in rows:
            session.expunge(row)
        return rows
