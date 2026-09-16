from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select

from katcha.acquisition.errors import ProviderFailure
from katcha.db import session_scope
from katcha.discovery_health_models import DiscoveryPollAttempt, DiscoverySourceState
from katcha.models import DomainEvent

_TERMINAL_OUTCOMES = {
    "success",
    "empty_success",
    "rate_limited",
    "transient_failure",
    "permanent_failure",
    "quota_exhausted",
    "deferred",
}


@dataclass(frozen=True, slots=True)
class SourcePollDecision:
    source_state_id: uuid.UUID
    source_key: str
    attempt_key: str
    allowed: bool
    reused: bool
    reason: str
    cursor: dict[str, Any]
    next_eligible_poll_at: datetime | None
    quota_used: int
    quota_limit_per_day: int | None


def _utc(value: datetime | None = None) -> datetime:
    current = value or datetime.now(UTC)
    if current.tzinfo is None:
        return current.replace(tzinfo=UTC)
    return current.astimezone(UTC)


def _canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def source_identity(
    adapter_key: str,
    adapter_version: str,
    effective_query: dict[str, Any],
) -> tuple[str, str]:
    query_fingerprint = hashlib.sha256(
        _canonical_json(effective_query).encode()
    ).hexdigest()
    source_key = hashlib.sha256(
        _canonical_json(
            {
                "adapter_key": adapter_key,
                "adapter_version": adapter_version,
                "query_fingerprint": query_fingerprint,
            }
        ).encode()
    ).hexdigest()
    return source_key, query_fingerprint


def deterministic_backoff_seconds(
    source_key: str,
    consecutive_failures: int,
    *,
    retry_after_seconds: int | None = None,
) -> int:
    failures = max(int(consecutive_failures), 1)
    exponential = min(60 * (2 ** min(failures - 1, 8)), 6 * 60 * 60)
    digest = hashlib.sha256(f"{source_key}:{failures}".encode()).digest()
    jitter = int(exponential * 0.15 * (int.from_bytes(digest[:2], "big") / 65535))
    calculated = exponential + jitter
    if retry_after_seconds is not None:
        return max(calculated, max(int(retry_after_seconds), 0))
    return calculated


def _start_of_utc_day(now: datetime) -> datetime:
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


def _reset_quota_window_if_needed(state: DiscoverySourceState, now: datetime) -> None:
    started = _utc(state.quota_window_started_at) if state.quota_window_started_at else None
    if started is None or started.date() != now.date():
        state.quota_used = 0
        state.quota_window_started_at = _start_of_utc_day(now)


def _next_quota_window(now: datetime) -> datetime:
    return _start_of_utc_day(now) + timedelta(days=1)


def _topic_watch_metadata(
    current: dict[str, Any], topic_watch_id: uuid.UUID | None
) -> dict[str, Any]:
    metadata = dict(current or {})
    ids = {
        str(value)
        for value in metadata.get("topic_watch_ids", [])
        if str(value).strip()
    }
    if topic_watch_id is not None:
        ids.add(str(topic_watch_id))
    metadata["topic_watch_ids"] = sorted(ids)
    return metadata


def get_or_create_source_state(
    *,
    adapter_key: str,
    adapter_version: str,
    effective_query: dict[str, Any],
    topic_watch_id: uuid.UUID | None = None,
    quota_limit_per_day: int | None = None,
) -> DiscoverySourceState:
    if quota_limit_per_day is not None and quota_limit_per_day < 1:
        raise ValueError("quota_limit_per_day must be positive")
    source_key, query_fingerprint = source_identity(
        adapter_key, adapter_version, effective_query
    )
    with session_scope() as session:
        state = session.scalar(
            select(DiscoverySourceState).where(
                DiscoverySourceState.source_key == source_key
            )
        )
        if state is None:
            state = DiscoverySourceState(
                source_key=source_key,
                adapter_key=adapter_key,
                adapter_version=adapter_version,
                query_fingerprint=query_fingerprint,
                effective_query=dict(effective_query),
                health_status="unknown",
                quota_limit_per_day=quota_limit_per_day,
                state_metadata=_topic_watch_metadata({}, topic_watch_id),
            )
            session.add(state)
            session.flush()
            session.add(
                DomainEvent(
                    aggregate_type="discovery_source",
                    aggregate_id=source_key,
                    event_type="discovery_source.created",
                    payload={
                        "source_key": source_key,
                        "adapter_key": adapter_key,
                        "adapter_version": adapter_version,
                    },
                )
            )
        else:
            state.state_metadata = _topic_watch_metadata(
                state.state_metadata, topic_watch_id
            )
            if quota_limit_per_day is not None:
                state.quota_limit_per_day = quota_limit_per_day
        session.flush()
        session.refresh(state)
        session.expunge(state)
        return state


def begin_source_poll(
    source_state_id: uuid.UUID,
    *,
    attempt_key: str,
    now: datetime | None = None,
) -> SourcePollDecision:
    key = attempt_key.strip()
    if not key:
        raise ValueError("attempt_key is required")
    if len(key) > 160:
        raise ValueError("attempt_key must be 160 characters or fewer")
    current = _utc(now)
    with session_scope() as session:
        state = session.get(DiscoverySourceState, source_state_id)
        if state is None:
            raise ValueError(f"discovery source state not found: {source_state_id}")
        existing = session.scalar(
            select(DiscoveryPollAttempt).where(
                DiscoveryPollAttempt.source_state_id == state.id,
                DiscoveryPollAttempt.attempt_key == key,
            )
        )
        if existing is not None:
            allowed = existing.outcome == "running"
            return SourcePollDecision(
                source_state_id=state.id,
                source_key=state.source_key,
                attempt_key=key,
                allowed=allowed,
                reused=True,
                reason=existing.outcome,
                cursor=dict(existing.cursor_before or state.cursor or {}),
                next_eligible_poll_at=state.next_eligible_poll_at,
                quota_used=state.quota_used,
                quota_limit_per_day=state.quota_limit_per_day,
            )

        _reset_quota_window_if_needed(state, current)
        eligible_at = (
            _utc(state.next_eligible_poll_at)
            if state.next_eligible_poll_at is not None
            else None
        )
        if eligible_at is not None and eligible_at > current:
            attempt = DiscoveryPollAttempt(
                source_state_id=state.id,
                attempt_key=key,
                outcome="deferred",
                cursor_before=dict(state.cursor or {}),
                cursor_after=dict(state.cursor or {}),
                attempt_metadata={"reason": "backoff"},
                completed_at=current,
            )
            session.add(attempt)
            session.add(
                DomainEvent(
                    aggregate_type="discovery_source",
                    aggregate_id=state.source_key,
                    event_type="discovery_source.poll_deferred",
                    payload={
                        "source_key": state.source_key,
                        "adapter_key": state.adapter_key,
                        "reason": "backoff",
                        "next_eligible_poll_at": eligible_at.isoformat(),
                    },
                )
            )
            return SourcePollDecision(
                source_state_id=state.id,
                source_key=state.source_key,
                attempt_key=key,
                allowed=False,
                reused=False,
                reason="deferred",
                cursor=dict(state.cursor or {}),
                next_eligible_poll_at=eligible_at,
                quota_used=state.quota_used,
                quota_limit_per_day=state.quota_limit_per_day,
            )

        quota_limit = state.quota_limit_per_day
        if quota_limit is not None and state.quota_used >= quota_limit:
            next_window = _next_quota_window(current)
            state.next_eligible_poll_at = next_window
            state.last_outcome = "quota_exhausted"
            attempt = DiscoveryPollAttempt(
                source_state_id=state.id,
                attempt_key=key,
                outcome="quota_exhausted",
                cursor_before=dict(state.cursor or {}),
                cursor_after=dict(state.cursor or {}),
                attempt_metadata={"reason": "daily_quota"},
                completed_at=current,
            )
            session.add(attempt)
            session.add(
                DomainEvent(
                    aggregate_type="discovery_source",
                    aggregate_id=state.source_key,
                    event_type="discovery_source.quota_exhausted",
                    payload={
                        "source_key": state.source_key,
                        "adapter_key": state.adapter_key,
                        "quota_used": state.quota_used,
                        "quota_limit_per_day": quota_limit,
                        "next_eligible_poll_at": next_window.isoformat(),
                    },
                )
            )
            return SourcePollDecision(
                source_state_id=state.id,
                source_key=state.source_key,
                attempt_key=key,
                allowed=False,
                reused=False,
                reason="quota_exhausted",
                cursor=dict(state.cursor or {}),
                next_eligible_poll_at=next_window,
                quota_used=state.quota_used,
                quota_limit_per_day=quota_limit,
            )

        state.quota_used += 1
        state.total_polls += 1
        state.last_poll_at = current
        attempt = DiscoveryPollAttempt(
            source_state_id=state.id,
            attempt_key=key,
            outcome="running",
            cursor_before=dict(state.cursor or {}),
            cursor_after=dict(state.cursor or {}),
            started_at=current,
        )
        session.add(attempt)
        session.flush()
        return SourcePollDecision(
            source_state_id=state.id,
            source_key=state.source_key,
            attempt_key=key,
            allowed=True,
            reused=False,
            reason="ready",
            cursor=dict(state.cursor or {}),
            next_eligible_poll_at=state.next_eligible_poll_at,
            quota_used=state.quota_used,
            quota_limit_per_day=state.quota_limit_per_day,
        )


def bind_source_poll_run(
    source_state_id: uuid.UUID,
    *,
    attempt_key: str,
    discovery_run_id: uuid.UUID,
) -> None:
    with session_scope() as session:
        attempt = session.scalar(
            select(DiscoveryPollAttempt).where(
                DiscoveryPollAttempt.source_state_id == source_state_id,
                DiscoveryPollAttempt.attempt_key == attempt_key,
            )
        )
        if attempt is None:
            raise ValueError("source poll attempt not found")
        if attempt.discovery_run_id is None:
            attempt.discovery_run_id = discovery_run_id
        elif attempt.discovery_run_id != discovery_run_id:
            raise ValueError("source poll attempt is already bound to another run")


def record_source_page_success(
    source_state_id: uuid.UUID,
    *,
    attempt_key: str,
    discovery_run_id: uuid.UUID,
    candidate_count: int,
    next_cursor: dict[str, Any],
    done: bool,
    now: datetime | None = None,
) -> str:
    current = _utc(now)
    with session_scope() as session:
        state = session.get(DiscoverySourceState, source_state_id)
        if state is None:
            raise ValueError(f"discovery source state not found: {source_state_id}")
        attempt = session.scalar(
            select(DiscoveryPollAttempt).where(
                DiscoveryPollAttempt.source_state_id == state.id,
                DiscoveryPollAttempt.attempt_key == attempt_key,
            )
        )
        if attempt is None:
            raise ValueError("source poll attempt not found")
        if attempt.outcome in _TERMINAL_OUTCOMES:
            return attempt.outcome
        if attempt.discovery_run_id is None:
            attempt.discovery_run_id = discovery_run_id
        attempt.candidate_count += max(int(candidate_count), 0)
        attempt.pages += 1
        attempt.cursor_after = dict(next_cursor or {})
        state.cursor = dict(next_cursor or {})
        if not done:
            return "running"

        outcome = "success" if attempt.candidate_count > 0 else "empty_success"
        prior_health = state.health_status
        attempt.outcome = outcome
        attempt.completed_at = current
        state.health_status = "healthy"
        state.consecutive_failures = 0
        state.last_outcome = outcome
        state.last_error_kind = None
        state.last_success_at = current
        state.next_eligible_poll_at = None
        state.rate_limit_reset_at = None
        if outcome == "success":
            state.total_successes += 1
        else:
            state.total_empty_successes += 1
        session.add(
            DomainEvent(
                aggregate_type="discovery_source",
                aggregate_id=state.source_key,
                event_type="discovery_source.poll_succeeded",
                payload={
                    "source_key": state.source_key,
                    "adapter_key": state.adapter_key,
                    "outcome": outcome,
                    "candidate_count": attempt.candidate_count,
                    "pages": attempt.pages,
                },
            )
        )
        if prior_health in {"degraded", "rate_limited", "unhealthy"}:
            session.add(
                DomainEvent(
                    aggregate_type="discovery_source",
                    aggregate_id=state.source_key,
                    event_type="discovery_source.recovered",
                    payload={
                        "source_key": state.source_key,
                        "adapter_key": state.adapter_key,
                        "previous_health": prior_health,
                    },
                )
            )
        return outcome


def record_source_failure(
    source_state_id: uuid.UUID,
    *,
    attempt_key: str,
    discovery_run_id: uuid.UUID | None,
    failure: ProviderFailure,
    now: datetime | None = None,
) -> str:
    current = _utc(now)
    with session_scope() as session:
        state = session.get(DiscoverySourceState, source_state_id)
        if state is None:
            raise ValueError(f"discovery source state not found: {source_state_id}")
        attempt = session.scalar(
            select(DiscoveryPollAttempt).where(
                DiscoveryPollAttempt.source_state_id == state.id,
                DiscoveryPollAttempt.attempt_key == attempt_key,
            )
        )
        if attempt is None:
            raise ValueError("source poll attempt not found")
        if attempt.outcome in _TERMINAL_OUTCOMES:
            return attempt.outcome
        if attempt.discovery_run_id is None and discovery_run_id is not None:
            attempt.discovery_run_id = discovery_run_id

        state.consecutive_failures += 1
        state.total_failures += 1
        state.last_failure_at = current
        state.last_error_kind = failure.kind
        attempt.http_status = failure.status_code
        attempt.retry_after_seconds = failure.retry_after_seconds
        attempt.error_kind = failure.kind
        attempt.completed_at = current

        if failure.kind == "rate_limited":
            outcome = "rate_limited"
            state.health_status = "rate_limited"
            state.total_rate_limits += 1
        elif failure.retryable:
            outcome = "transient_failure"
            state.health_status = "degraded"
        else:
            outcome = "permanent_failure"
            state.health_status = "unhealthy"

        attempt.outcome = outcome
        state.last_outcome = outcome
        backoff_seconds = deterministic_backoff_seconds(
            state.source_key,
            state.consecutive_failures,
            retry_after_seconds=failure.retry_after_seconds,
        )
        state.next_eligible_poll_at = current + timedelta(seconds=backoff_seconds)
        if outcome == "rate_limited":
            state.rate_limit_reset_at = state.next_eligible_poll_at

        event_type = {
            "rate_limited": "discovery_source.rate_limited",
            "transient_failure": "discovery_source.poll_failed",
            "permanent_failure": "discovery_source.degraded",
        }[outcome]
        session.add(
            DomainEvent(
                aggregate_type="discovery_source",
                aggregate_id=state.source_key,
                event_type=event_type,
                payload={
                    "source_key": state.source_key,
                    "adapter_key": state.adapter_key,
                    "outcome": outcome,
                    "error_kind": failure.kind,
                    "http_status": failure.status_code,
                    "retry_after_seconds": failure.retry_after_seconds,
                    "consecutive_failures": state.consecutive_failures,
                    "next_eligible_poll_at": state.next_eligible_poll_at.isoformat(),
                },
            )
        )
        return outcome


def get_source_state(source_key: str) -> DiscoverySourceState | None:
    with session_scope() as session:
        state = session.scalar(
            select(DiscoverySourceState).where(
                DiscoverySourceState.source_key == source_key
            )
        )
        if state is not None:
            session.expunge(state)
        return state


def list_source_states(
    *,
    topic_watch_id: uuid.UUID | None = None,
    health_status: str | None = None,
    limit: int = 100,
) -> list[DiscoverySourceState]:
    bounded = max(1, min(int(limit), 500))
    with session_scope() as session:
        query = select(DiscoverySourceState).order_by(
            DiscoverySourceState.updated_at.desc()
        )
        if health_status:
            query = query.where(DiscoverySourceState.health_status == health_status)
        rows = list(session.scalars(query.limit(500)))
        if topic_watch_id is not None:
            watch_id = str(topic_watch_id)
            rows = [
                row
                for row in rows
                if watch_id
                in {
                    str(value)
                    for value in (row.state_metadata or {}).get("topic_watch_ids", [])
                }
            ]
        rows = rows[:bounded]
        for row in rows:
            session.expunge(row)
        return rows


def list_poll_attempts(
    source_state_id: uuid.UUID,
    *,
    limit: int = 100,
) -> list[DiscoveryPollAttempt]:
    bounded = max(1, min(int(limit), 500))
    with session_scope() as session:
        rows = list(
            session.scalars(
                select(DiscoveryPollAttempt)
                .where(DiscoveryPollAttempt.source_state_id == source_state_id)
                .order_by(DiscoveryPollAttempt.started_at.desc())
                .limit(bounded)
            )
        )
        for row in rows:
            session.expunge(row)
        return rows


def source_health_summary(
    *, topic_watch_id: uuid.UUID | None = None
) -> dict[str, int]:
    rows = list_source_states(topic_watch_id=topic_watch_id, limit=500)
    summary: dict[str, int] = {"total": len(rows)}
    for row in rows:
        summary[row.health_status] = summary.get(row.health_status, 0) + 1
    return summary


def reset_source_state(
    source_key: str,
    *,
    actor: str = "operator",
    reset_quota: bool = False,
) -> DiscoverySourceState:
    with session_scope() as session:
        state = session.scalar(
            select(DiscoverySourceState).where(
                DiscoverySourceState.source_key == source_key
            )
        )
        if state is None:
            raise ValueError(f"discovery source not found: {source_key}")
        state.health_status = "unknown"
        state.consecutive_failures = 0
        state.last_error_kind = None
        state.next_eligible_poll_at = None
        state.rate_limit_reset_at = None
        if reset_quota:
            state.quota_used = 0
            state.quota_window_started_at = _start_of_utc_day(datetime.now(UTC))
        session.add(
            DomainEvent(
                aggregate_type="discovery_source",
                aggregate_id=state.source_key,
                event_type="discovery_source.reset",
                actor=actor,
                payload={
                    "source_key": state.source_key,
                    "adapter_key": state.adapter_key,
                    "reset_quota": reset_quota,
                },
            )
        )
        session.flush()
        session.refresh(state)
        session.expunge(state)
        return state
