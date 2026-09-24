from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import and_, func, select

from katcha.acquisition_models import DiscoveryRun, TopicWatchVersion
from katcha.config import get_settings
from katcha.db import session_scope
from katcha.discovery_trend_models import TrendWatchSourceState
from katcha.models import DomainEvent

_RETRY_AFTER_RE = re.compile(r"retry[_ -]?after(?:_seconds)?[=: ]+(\d+)", re.IGNORECASE)
_STATUS_RE = re.compile(r"status\s+(\d{3})", re.IGNORECASE)


def watch_scope_key(channel_profile_id: uuid.UUID | None) -> str:
    return f"channel:{channel_profile_id}" if channel_profile_id else "global"


def _utc(value: datetime | None = None) -> datetime:
    value = value or datetime.now(UTC)
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _adapter_identity(raw: dict[str, Any]) -> tuple[str, str]:
    return (
        str(raw.get("adapter_key") or "").strip().casefold(),
        str(raw.get("adapter_version") or "v1").strip(),
    )


def ensure_topic_watch_source_states(topic_watch_id: uuid.UUID) -> list[TrendWatchSourceState]:
    with session_scope() as session:
        watch = session.get(TopicWatchVersion, topic_watch_id)
        if watch is None:
            raise ValueError(f"topic watch version not found: {topic_watch_id}")
        states: list[TrendWatchSourceState] = []
        for index, raw in enumerate(watch.adapter_configs or []):
            adapter_key, adapter_version = _adapter_identity(dict(raw))
            if not adapter_key:
                continue
            state = session.scalar(
                select(TrendWatchSourceState).where(
                    TrendWatchSourceState.topic_watch_id == topic_watch_id,
                    TrendWatchSourceState.adapter_index == index,
                )
            )
            if state is None:
                state = TrendWatchSourceState(
                    topic_watch_id=topic_watch_id,
                    adapter_index=index,
                    adapter_key=adapter_key,
                    adapter_version=adapter_version,
                    health_status="unknown",
                    consecutive_failures=0,
                    cursor={},
                    state_metadata={},
                )
                session.add(state)
                session.flush()
            states.append(state)
        return states


def source_state_snapshot(
    topic_watch_id: uuid.UUID,
    adapter_index: int,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    reference = _utc(now)
    ensure_topic_watch_source_states(topic_watch_id)
    with session_scope() as session:
        state = session.scalar(
            select(TrendWatchSourceState).where(
                TrendWatchSourceState.topic_watch_id == topic_watch_id,
                TrendWatchSourceState.adapter_index == adapter_index,
            )
        )
        if state is None:
            return {"cursor": {}, "backoff_active": False, "backoff_until": None}
        until = _utc(state.backoff_until) if state.backoff_until else None
        return {
            "cursor": dict(state.cursor or {}),
            "backoff_active": bool(until and until > reference),
            "backoff_until": until,
            "health_status": state.health_status,
        }


def _classify_failure(message: str) -> tuple[str, bool, int | None]:
    lowered = message.casefold()
    if "discovery daily budget exhausted" in lowered:
        return "quota_deferred", True, None
    retry_match = _RETRY_AFTER_RE.search(message)
    retry_after = int(retry_match.group(1)) if retry_match else None
    status_match = _STATUS_RE.search(message)
    status_code = int(status_match.group(1)) if status_match else None
    if status_code == 429 or "rate limit" in lowered or "rate_limited" in lowered:
        return "rate_limited", True, retry_after
    if status_code is not None and status_code >= 500:
        return "provider_unavailable", True, retry_after
    if "timeout" in lowered or "transport" in lowered or "request failed" in lowered:
        return "transport_error", True, retry_after
    if status_code in {401, 403} or "not configured" in lowered:
        return "provider_configuration", False, retry_after
    if "invalid" in lowered or "unsupported" in lowered:
        return "invalid_provider_response", False, retry_after
    return "provider_error", True, retry_after


def _backoff_seconds(
    failure_count: int,
    *,
    requested_retry_after: int | None,
) -> int:
    settings = get_settings()
    computed = 300 * (2 ** min(max(failure_count - 1, 0), 6))
    return min(
        settings.trend_source_max_backoff_seconds,
        max(60, computed, int(requested_retry_after or 0)),
    )


def _run_source_identity(run: DiscoveryRun) -> tuple[uuid.UUID, int] | None:
    metadata = dict(run.run_metadata or {})
    raw_watch_id = metadata.get("topic_watch_id")
    raw_index = metadata.get("adapter_index")
    if raw_watch_id is None or raw_index is None:
        return None
    try:
        return uuid.UUID(str(raw_watch_id)), int(raw_index)
    except (TypeError, ValueError):
        return None


def record_discovery_run_success(run_id: uuid.UUID) -> None:
    now = datetime.now(UTC)
    with session_scope() as session:
        run = session.get(DiscoveryRun, run_id)
        if run is None:
            return
        identity = _run_source_identity(run)
        if identity is None:
            return
        topic_watch_id, adapter_index = identity
        state = session.scalar(
            select(TrendWatchSourceState).where(
                TrendWatchSourceState.topic_watch_id == topic_watch_id,
                TrendWatchSourceState.adapter_index == adapter_index,
            )
        )
        if state is None:
            state = TrendWatchSourceState(
                topic_watch_id=topic_watch_id,
                adapter_index=adapter_index,
                adapter_key=run.adapter_key,
                adapter_version=run.adapter_version,
            )
            session.add(state)
        state.health_status = "healthy"
        state.consecutive_failures = 0
        state.cursor = dict(run.cursor or {})
        state.last_discovery_run_id = run.id
        state.last_success_at = now
        state.backoff_until = None
        state.last_error_kind = None
        state.last_error_summary = None
        session.add(
            DomainEvent(
                aggregate_type="topic_watch_source",
                aggregate_id=f"{topic_watch_id}:{adapter_index}",
                event_type="topic_watch.source_healthy",
                payload={
                    "topic_watch_id": str(topic_watch_id),
                    "adapter_index": adapter_index,
                    "adapter_key": run.adapter_key,
                    "discovery_run_id": str(run.id),
                },
            )
        )


def record_discovery_run_failure(run_id: uuid.UUID, message: str) -> None:
    now = datetime.now(UTC)
    with session_scope() as session:
        run = session.get(DiscoveryRun, run_id)
        if run is None:
            return
        identity = _run_source_identity(run)
        if identity is None:
            return
        topic_watch_id, adapter_index = identity
        state = session.scalar(
            select(TrendWatchSourceState).where(
                TrendWatchSourceState.topic_watch_id == topic_watch_id,
                TrendWatchSourceState.adapter_index == adapter_index,
            )
        )
        if state is None:
            state = TrendWatchSourceState(
                topic_watch_id=topic_watch_id,
                adapter_index=adapter_index,
                adapter_key=run.adapter_key,
                adapter_version=run.adapter_version,
            )
            session.add(state)
        kind, transient, retry_after = _classify_failure(message)
        failures = int(state.consecutive_failures or 0) + 1
        delay = _backoff_seconds(failures, requested_retry_after=retry_after)
        state.consecutive_failures = failures
        state.last_discovery_run_id = run.id
        state.last_failure_at = now
        state.backoff_until = now + timedelta(seconds=delay)
        state.last_error_kind = kind
        state.last_error_summary = message[:1000]
        if kind == "rate_limited":
            state.health_status = "rate_limited"
        elif not transient or failures >= 3:
            state.health_status = "down"
        else:
            state.health_status = "degraded"
        session.add(
            DomainEvent(
                aggregate_type="topic_watch_source",
                aggregate_id=f"{topic_watch_id}:{adapter_index}",
                event_type="topic_watch.source_degraded",
                payload={
                    "topic_watch_id": str(topic_watch_id),
                    "adapter_index": adapter_index,
                    "adapter_key": run.adapter_key,
                    "discovery_run_id": str(run.id),
                    "health_status": state.health_status,
                    "error_kind": kind,
                    "consecutive_failures": failures,
                    "retry_after_seconds": delay,
                },
            )
        )


def _summary(states: list[TrendWatchSourceState]) -> dict[str, Any]:
    if not states:
        return {
            "configured": 0,
            "healthy": 0,
            "degraded": 0,
            "unknown": 0,
            "coverage": 1.0,
        }
    healthy = sum(1 for item in states if item.health_status == "healthy")
    unknown = sum(1 for item in states if item.health_status == "unknown")
    degraded = len(states) - healthy - unknown
    coverage = (healthy + 0.5 * unknown) / len(states)
    return {
        "configured": len(states),
        "healthy": healthy,
        "degraded": degraded,
        "unknown": unknown,
        "coverage": round(coverage, 6),
    }


def topic_watch_source_health(topic_watch_id: uuid.UUID) -> dict[str, Any]:
    ensure_topic_watch_source_states(topic_watch_id)
    with session_scope() as session:
        states = list(
            session.scalars(
                select(TrendWatchSourceState)
                .where(TrendWatchSourceState.topic_watch_id == topic_watch_id)
                .order_by(TrendWatchSourceState.adapter_index)
            )
        )
        summary = _summary(states)
        summary["topic_watch_id"] = str(topic_watch_id)
        summary["sources"] = [
            {
                "adapter_index": state.adapter_index,
                "adapter_key": state.adapter_key,
                "adapter_version": state.adapter_version,
                "health_status": state.health_status,
                "consecutive_failures": state.consecutive_failures,
                "last_success_at": state.last_success_at.isoformat()
                if state.last_success_at
                else None,
                "last_failure_at": state.last_failure_at.isoformat()
                if state.last_failure_at
                else None,
                "backoff_until": state.backoff_until.isoformat() if state.backoff_until else None,
                "last_error_kind": state.last_error_kind,
                "last_discovery_run_id": str(state.last_discovery_run_id)
                if state.last_discovery_run_id
                else None,
                "cursor": dict(state.cursor or {}),
            }
            for state in states
        ]
        return summary


def channel_trend_source_health(channel_profile_id: uuid.UUID) -> dict[str, Any]:
    scope = watch_scope_key(channel_profile_id)
    with session_scope() as session:
        latest = (
            select(
                TopicWatchVersion.watch_key.label("watch_key"),
                func.max(TopicWatchVersion.version).label("version"),
            )
            .where(TopicWatchVersion.scope_key == scope)
            .group_by(TopicWatchVersion.watch_key)
            .subquery()
        )
        watches = list(
            session.scalars(
                select(TopicWatchVersion)
                .join(
                    latest,
                    and_(
                        TopicWatchVersion.watch_key == latest.c.watch_key,
                        TopicWatchVersion.version == latest.c.version,
                    ),
                )
                .where(
                    TopicWatchVersion.scope_key == scope,
                    TopicWatchVersion.enabled.is_(True),
                )
            )
        )
    all_states: list[TrendWatchSourceState] = []
    for watch in watches:
        ensure_topic_watch_source_states(watch.id)
        with session_scope() as session:
            all_states.extend(
                list(
                    session.scalars(
                        select(TrendWatchSourceState).where(
                            TrendWatchSourceState.topic_watch_id == watch.id
                        )
                    )
                )
            )
    summary = _summary(all_states)
    summary["channel_profile_id"] = str(channel_profile_id)
    summary["watch_count"] = len(watches)
    return summary


def source_health_allows_refresh(
    summary: dict[str, Any],
    *,
    minimum_coverage: float,
) -> bool:
    configured = max(0, int(summary.get("configured") or 0))
    if configured == 0:
        return True
    coverage = max(0.0, min(1.0, float(summary.get("coverage") or 0.0)))
    return coverage >= max(0.0, min(1.0, minimum_coverage))
