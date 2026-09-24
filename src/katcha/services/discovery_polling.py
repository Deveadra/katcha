from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from katcha.acquisition_models import DiscoveryRun
from katcha.config import get_settings
from katcha.db import session_scope
from katcha.discovery_poll_models import (
    DiscoveryCollectionClaim,
    DiscoveryProviderQuotaWindow,
    DiscoveryQuotaReservation,
    TrendWatchPollAttempt,
)
from katcha.discovery_trend_models import TrendWatchSourceState
from katcha.domain import DiscoveryRunStatus
from katcha.models import DomainEvent

_TERMINAL_ATTEMPT_OUTCOMES = {
    "success",
    "empty_success",
    "rate_limited",
    "transient_failure",
    "permanent_failure",
    "quota_exhausted",
    "deferred",
    "shared_reuse",
    "abandoned",
}


@dataclass(frozen=True, slots=True)
class PollStartDecision:
    attempt_id: uuid.UUID
    source_state_id: uuid.UUID
    source_identity: str
    action: str
    reason: str
    discovery_run_id: uuid.UUID | None
    cursor: dict[str, Any]


@dataclass(frozen=True, slots=True)
class PageQuotaDecision:
    allowed: bool
    page_key: str
    reason: str
    blocked_until: datetime | None = None


@dataclass(frozen=True, slots=True)
class QuotaDemand:
    provider_key: str
    bucket_key: str
    units: int
    limit_units: int
    window_start: datetime
    window_end: datetime
    window_key: str


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
) -> str:
    return hashlib.sha256(
        _canonical_json(
            {
                "adapter_key": adapter_key.strip().casefold(),
                "adapter_version": adapter_version.strip(),
                "query": effective_query,
            }
        ).encode()
    ).hexdigest()


def collection_window_key(
    now: datetime | None = None,
    *,
    window_seconds: int | None = None,
) -> str:
    settings = get_settings()
    seconds = window_seconds or settings.trend_collection_dedupe_window_seconds
    if seconds < 1:
        raise ValueError("collection dedupe window must be positive")
    current = _utc(now)
    epoch = int(current.timestamp())
    start = epoch - (epoch % seconds)
    return f"{start}:{seconds}"


def page_identity(cursor: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(cursor).encode()).hexdigest()


def deterministic_backoff_seconds(
    identity: str,
    consecutive_failures: int,
    *,
    retry_after_seconds: int | None = None,
) -> int:
    settings = get_settings()
    failures = max(int(consecutive_failures), 1)
    base = min(300 * (2 ** min(failures - 1, 6)), settings.trend_source_max_backoff_seconds)
    digest = hashlib.sha256(f"{identity}:{failures}".encode()).digest()
    jitter = int(base * 0.15 * (int.from_bytes(digest[:2], "big") / 65535))
    calculated = min(base + jitter, settings.trend_source_max_backoff_seconds)
    if retry_after_seconds is not None:
        calculated = max(calculated, max(int(retry_after_seconds), 0))
    return calculated


def _source_quota_limit(config: dict[str, Any]) -> int | None:
    raw = config.get("source_quota_limit_per_day")
    if raw is None:
        return None
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError("source_quota_limit_per_day must be an integer") from exc
    if value < 1:
        raise ValueError("source_quota_limit_per_day must be positive")
    return value


def _quota_override(
    config: dict[str, Any],
    bucket_key: str,
) -> tuple[int, int | None] | None:
    raw_limits = config.get("provider_quota_limits")
    if not isinstance(raw_limits, dict) or bucket_key not in raw_limits:
        return None
    raw = raw_limits[bucket_key]
    if isinstance(raw, dict):
        try:
            limit = int(raw["limit"])
            seconds = int(raw.get("window_seconds") or 86400)
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(
                f"provider quota {bucket_key} requires integer limit/window_seconds"
            ) from exc
        if limit < 1 or seconds < 1:
            raise ValueError(f"provider quota {bucket_key} must be positive")
        return limit, seconds
    try:
        limit = int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"provider quota {bucket_key} must be an integer or object") from exc
    if limit < 1:
        raise ValueError(f"provider quota {bucket_key} must be positive")
    return limit, 86400


def _daily_window(
    now: datetime,
    *,
    timezone: str,
    bucket_key: str,
) -> tuple[datetime, datetime, str]:
    zone = ZoneInfo(timezone)
    local = now.astimezone(zone)
    start_local = local.replace(hour=0, minute=0, second=0, microsecond=0)
    end_local = start_local + timedelta(days=1)
    start = start_local.astimezone(UTC)
    end = end_local.astimezone(UTC)
    return start, end, f"{bucket_key}:{start_local.date().isoformat()}:{timezone}"


def _fixed_window(
    now: datetime,
    *,
    seconds: int,
    bucket_key: str,
) -> tuple[datetime, datetime, str]:
    epoch = int(now.timestamp())
    start_epoch = epoch - (epoch % seconds)
    start = datetime.fromtimestamp(start_epoch, tz=UTC)
    end = start + timedelta(seconds=seconds)
    return start, end, f"{bucket_key}:{start_epoch}:{seconds}"


def provider_page_demands(
    adapter_key: str,
    config: dict[str, Any],
    *,
    now: datetime | None = None,
) -> list[QuotaDemand]:
    current = _utc(now)
    settings = get_settings()
    key = adapter_key.strip().casefold()
    specs: list[tuple[str, str, int, int, str | int]] = []
    if key == "youtube":
        specs = [
            (
                "youtube",
                "youtube.search.list",
                1,
                settings.trend_youtube_search_daily_limit,
                "America/Los_Angeles",
            ),
            (
                "youtube",
                "youtube.core",
                1,
                settings.trend_youtube_core_daily_limit,
                "America/Los_Angeles",
            ),
        ]
    elif key == "reddit":
        api_override = _quota_override(config, "reddit.api")
        oauth_override = _quota_override(config, "reddit.oauth")
        if api_override is not None:
            specs.append(
                ("reddit", "reddit.api", 1, api_override[0], api_override[1])
            )
        if oauth_override is not None:
            specs.append(
                ("reddit", "reddit.oauth", 1, oauth_override[0], oauth_override[1])
            )
    elif key == "rss_atom":
        override = _quota_override(config, "rss.http")
        if override is not None:
            # Reserve the initial fetch plus the maximum three redirects.
            specs = [("rss_atom", "rss.http", 4, override[0], override[1])]

    result: list[QuotaDemand] = []
    for provider, bucket, units, default_limit, window in specs:
        override = _quota_override(config, bucket)
        limit = override[0] if override is not None else default_limit
        if override is not None:
            window = override[1]
        if isinstance(window, str):
            start, end, window_key = _daily_window(
                current, timezone=window, bucket_key=bucket
            )
        else:
            start, end, window_key = _fixed_window(
                current, seconds=window, bucket_key=bucket
            )
        result.append(
            QuotaDemand(
                provider_key=provider,
                bucket_key=bucket,
                units=units,
                limit_units=limit,
                window_start=start,
                window_end=end,
                window_key=window_key,
            )
        )
    return result


def _source_day_start(now: datetime) -> datetime:
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


def _attempt_source_usage(
    session: Session,
    state: TrendWatchSourceState,
    now: datetime,
) -> int:
    window_start = _source_day_start(now)
    raw_reset = dict(state.state_metadata or {}).get("source_quota_reset_at")
    if isinstance(raw_reset, str):
        try:
            reset_at = _utc(datetime.fromisoformat(raw_reset.replace("Z", "+00:00")))
        except ValueError:
            reset_at = None
        if reset_at is not None and reset_at > window_start:
            window_start = reset_at
    return int(
        session.scalar(
            select(
                func.coalesce(
                    func.sum(
                        TrendWatchPollAttempt.source_quota_reserved
                        + TrendWatchPollAttempt.source_quota_consumed
                    ),
                    0,
                )
            ).where(
                TrendWatchPollAttempt.source_state_id == state.id,
                TrendWatchPollAttempt.started_at >= window_start,
            )
        )
        or 0
    )


def _attempt_decision(attempt: TrendWatchPollAttempt) -> PollStartDecision:
    if attempt.outcome == "shared_reuse":
        action = "reuse"
    elif attempt.outcome in {"reserved", "running"}:
        action = "execute"
    else:
        action = "defer"
    return PollStartDecision(
        attempt_id=attempt.id,
        source_state_id=attempt.source_state_id,
        source_identity=attempt.source_identity,
        action=action,
        reason=attempt.outcome,
        discovery_run_id=attempt.discovery_run_id,
        cursor=dict(attempt.cursor_before or {}),
    )


def _consume_ambiguous_reservations(
    session: Session,
    attempt: TrendWatchPollAttempt,
    now: datetime,
) -> None:
    reservations = list(
        session.scalars(
            select(DiscoveryQuotaReservation)
            .where(
                DiscoveryQuotaReservation.poll_attempt_id == attempt.id,
                DiscoveryQuotaReservation.status == "reserved",
            )
            .with_for_update()
        )
    )
    for reservation in reservations:
        window = session.get(DiscoveryProviderQuotaWindow, reservation.quota_window_id)
        if window is None:
            continue
        window.reserved_units = max(
            0, int(window.reserved_units) - int(reservation.reserved_units)
        )
        window.used_units += int(reservation.reserved_units)
        reservation.consumed_units = int(reservation.reserved_units)
        reservation.status = "consumed_ambiguous"
        reservation.settled_at = now
    attempt.source_quota_consumed += int(attempt.source_quota_reserved)
    attempt.source_quota_reserved = 0
    attempt.outcome = "abandoned"
    attempt.completed_at = now


def begin_poll_attempt(
    source_state_id: uuid.UUID,
    *,
    execution_key: str,
    adapter_key: str,
    adapter_version: str,
    effective_query: dict[str, Any],
    adapter_config: dict[str, Any],
    now: datetime | None = None,
) -> PollStartDecision:
    current = _utc(now)
    key = execution_key.strip()
    if not key:
        raise ValueError("execution_key is required")
    if len(key) > 160:
        raise ValueError("execution_key must be 160 characters or fewer")
    identity = source_identity(adapter_key, adapter_version, effective_query)
    quota_limit = _source_quota_limit(adapter_config)
    settings = get_settings()

    with session_scope() as session:
        state = session.scalar(
            select(TrendWatchSourceState)
            .where(TrendWatchSourceState.id == source_state_id)
            .with_for_update()
        )
        if state is None:
            raise ValueError(f"trend source state not found: {source_state_id}")

        existing = session.scalar(
            select(TrendWatchPollAttempt).where(
                TrendWatchPollAttempt.source_state_id == source_state_id,
                TrendWatchPollAttempt.execution_key == key,
            )
        )
        if existing is not None:
            session.expunge(existing)
            return _attempt_decision(existing)

        state.source_identity = identity
        state.source_quota_limit_per_day = quota_limit
        state.last_poll_at = current
        backoff_until = _utc(state.backoff_until) if state.backoff_until else None
        if backoff_until is not None and backoff_until > current:
            attempt = TrendWatchPollAttempt(
                source_state_id=state.id,
                execution_key=key,
                source_identity=identity,
                outcome="deferred",
                cursor_before=dict(state.cursor or {}),
                cursor_after=dict(state.cursor or {}),
                attempt_metadata={
                    "reason": "source_backoff",
                    "backoff_until": backoff_until.isoformat(),
                },
                completed_at=current,
                started_at=current,
            )
            session.add(attempt)
            session.flush()
            session.expunge(attempt)
            return _attempt_decision(attempt)

        if quota_limit is not None:
            usage = _attempt_source_usage(session, state, current)
            if usage >= quota_limit:
                next_eligible = _source_day_start(current) + timedelta(days=1)
                state.health_status = "quota_exhausted"
                state.backoff_until = next_eligible
                state.last_poll_at = current
                state.last_error_kind = "source_daily_quota"
                state.last_error_summary = "source daily poll quota exhausted"
                attempt = TrendWatchPollAttempt(
                    source_state_id=state.id,
                    execution_key=key,
                    source_identity=identity,
                    outcome="quota_exhausted",
                    cursor_before=dict(state.cursor or {}),
                    cursor_after=dict(state.cursor or {}),
                    attempt_metadata={
                        "reason": "source_daily_quota",
                        "quota_limit_per_day": quota_limit,
                        "quota_used": usage,
                        "next_eligible_poll_at": next_eligible.isoformat(),
                    },
                    completed_at=current,
                    started_at=current,
                )
                session.add(attempt)
                session.flush()
                session.add(
                    DomainEvent(
                        aggregate_type="topic_watch_source",
                        aggregate_id=str(state.id),
                        event_type="topic_watch.quota_exhausted",
                        payload={
                            "source_state_id": str(state.id),
                            "poll_attempt_id": str(attempt.id),
                            "reason": "source_daily_quota",
                            "next_eligible_poll_at": next_eligible.isoformat(),
                        },
                    )
                )
                session.expunge(attempt)
                return _attempt_decision(attempt)

        window_key = collection_window_key(current)
        claim = session.scalar(
            select(DiscoveryCollectionClaim)
            .where(
                DiscoveryCollectionClaim.source_identity == identity,
                DiscoveryCollectionClaim.collection_window_key == window_key,
            )
            .with_for_update()
        )
        claim_created = False
        if claim is None:
            claim_id = uuid.uuid4()
            inserted = session.execute(
                pg_insert(DiscoveryCollectionClaim)
                .values(
                    id=claim_id,
                    source_identity=identity,
                    collection_window_key=window_key,
                    owner_source_state_id=state.id,
                    status="running",
                    lease_expires_at=current
                    + timedelta(seconds=settings.trend_poll_lease_seconds),
                    claim_metadata={},
                )
                .on_conflict_do_nothing(
                    index_elements=["source_identity", "collection_window_key"]
                )
                .returning(DiscoveryCollectionClaim.id)
            ).scalar_one_or_none()
            claim_created = inserted is not None
            claim = session.scalar(
                select(DiscoveryCollectionClaim)
                .where(
                    DiscoveryCollectionClaim.source_identity == identity,
                    DiscoveryCollectionClaim.collection_window_key == window_key,
                )
                .with_for_update()
            )
            if claim is None:
                raise RuntimeError("discovery collection claim was not persisted")

        if claim.status == "completed" and claim.discovery_run_id is not None:
            run = session.get(DiscoveryRun, claim.discovery_run_id)
            cursor = dict(run.cursor or {}) if run is not None else dict(state.cursor or {})
            prior_health = state.health_status
            state.cursor = cursor
            state.health_status = "healthy"
            state.consecutive_failures = 0
            state.last_discovery_run_id = claim.discovery_run_id
            state.last_success_at = current
            state.last_poll_at = current
            state.backoff_until = None
            state.rate_limit_reset_at = None
            state.last_error_kind = None
            state.last_error_summary = None
            attempt = TrendWatchPollAttempt(
                source_state_id=state.id,
                collection_claim_id=claim.id,
                discovery_run_id=claim.discovery_run_id,
                execution_key=key,
                source_identity=identity,
                outcome="shared_reuse",
                cursor_before=cursor,
                cursor_after=cursor,
                attempt_metadata={"reason": "shared_completed_collection"},
                completed_at=current,
                started_at=current,
            )
            session.add(attempt)
            session.flush()
            session.add(
                DomainEvent(
                    aggregate_type="topic_watch_source",
                    aggregate_id=str(state.id),
                    event_type="topic_watch.poll_reused",
                    payload={
                        "source_state_id": str(state.id),
                        "poll_attempt_id": str(attempt.id),
                        "discovery_run_id": str(claim.discovery_run_id),
                        "source_identity": identity,
                    },
                )
            )
            if prior_health in {"degraded", "rate_limited", "down", "quota_exhausted"}:
                session.add(
                    DomainEvent(
                        aggregate_type="topic_watch_source",
                        aggregate_id=str(state.id),
                        event_type="topic_watch.source_recovered",
                        payload={
                            "source_state_id": str(state.id),
                            "poll_attempt_id": str(attempt.id),
                            "previous_health": prior_health,
                            "outcome": "shared_reuse",
                        },
                    )
                )
            session.expunge(attempt)
            return _attempt_decision(attempt)

        lease_expires = _utc(claim.lease_expires_at)
        blocked_until = _utc(claim.blocked_until) if claim.blocked_until else None
        if blocked_until is not None and blocked_until > current:
            attempt = TrendWatchPollAttempt(
                source_state_id=state.id,
                collection_claim_id=claim.id,
                execution_key=key,
                source_identity=identity,
                outcome="deferred",
                cursor_before=dict(state.cursor or {}),
                cursor_after=dict(state.cursor or {}),
                attempt_metadata={
                    "reason": "shared_source_backoff",
                    "blocked_until": blocked_until.isoformat(),
                },
                completed_at=current,
                started_at=current,
            )
            session.add(attempt)
            session.flush()
            session.expunge(attempt)
            return _attempt_decision(attempt)

        if not claim_created and claim.status == "running" and lease_expires > current:
            attempt = TrendWatchPollAttempt(
                source_state_id=state.id,
                collection_claim_id=claim.id,
                execution_key=key,
                source_identity=identity,
                outcome="deferred",
                cursor_before=dict(state.cursor or {}),
                cursor_after=dict(state.cursor or {}),
                attempt_metadata={"reason": "shared_poll_in_progress"},
                completed_at=current,
                started_at=current,
            )
            session.add(attempt)
            session.flush()
            session.expunge(attempt)
            return _attempt_decision(attempt)

        stale_attempt = None if claim_created else session.scalar(
            select(TrendWatchPollAttempt)
            .where(
                TrendWatchPollAttempt.collection_claim_id == claim.id,
                TrendWatchPollAttempt.outcome.in_(("reserved", "running")),
            )
            .with_for_update()
        )
        if stale_attempt is not None:
            _consume_ambiguous_reservations(session, stale_attempt, current)

        claim.owner_source_state_id = state.id
        claim.discovery_run_id = None
        claim.status = "running"
        claim.lease_expires_at = current + timedelta(
            seconds=settings.trend_poll_lease_seconds
        )
        claim.blocked_until = None
        claim.completed_at = None

        attempt = TrendWatchPollAttempt(
            source_state_id=state.id,
            collection_claim_id=claim.id,
            execution_key=key,
            source_identity=identity,
            outcome="reserved",
            cursor_before=dict(state.cursor or {}),
            cursor_after=dict(state.cursor or {}),
            source_quota_reserved=1,
            attempt_metadata={"collection_window_key": window_key},
            started_at=current,
        )
        session.add(attempt)
        session.flush()
        session.add(
            DomainEvent(
                aggregate_type="topic_watch_source",
                aggregate_id=str(state.id),
                event_type="topic_watch.poll_reserved",
                payload={
                    "source_state_id": str(state.id),
                    "poll_attempt_id": str(attempt.id),
                    "adapter_key": adapter_key,
                    "source_identity": identity,
                },
            )
        )
        session.refresh(attempt)
        session.expunge(attempt)
        return _attempt_decision(attempt)


def bind_poll_attempt_run(
    poll_attempt_id: uuid.UUID,
    discovery_run_id: uuid.UUID,
) -> None:
    with session_scope() as session:
        attempt = session.scalar(
            select(TrendWatchPollAttempt)
            .where(TrendWatchPollAttempt.id == poll_attempt_id)
            .with_for_update()
        )
        if attempt is None:
            raise ValueError(f"poll attempt not found: {poll_attempt_id}")
        if attempt.discovery_run_id not in {None, discovery_run_id}:
            raise ValueError("poll attempt is already bound to another discovery run")
        attempt.discovery_run_id = discovery_run_id
        attempt.outcome = "running"
        if attempt.collection_claim_id is not None:
            claim = session.get(DiscoveryCollectionClaim, attempt.collection_claim_id)
            if claim is not None:
                claim.discovery_run_id = discovery_run_id


def _ensure_quota_window(
    session: Session,
    demand: QuotaDemand,
) -> DiscoveryProviderQuotaWindow:
    window_id = uuid.uuid4()
    session.execute(
        pg_insert(DiscoveryProviderQuotaWindow)
        .values(
            id=window_id,
            provider_key=demand.provider_key,
            bucket_key=demand.bucket_key,
            window_key=demand.window_key,
            window_start=demand.window_start,
            window_end=demand.window_end,
            limit_units=demand.limit_units,
            used_units=0,
            reserved_units=0,
            quota_metadata={},
        )
        .on_conflict_do_nothing(
            index_elements=["provider_key", "bucket_key", "window_key"]
        )
    )
    row = session.scalar(
        select(DiscoveryProviderQuotaWindow)
        .where(
            DiscoveryProviderQuotaWindow.provider_key == demand.provider_key,
            DiscoveryProviderQuotaWindow.bucket_key == demand.bucket_key,
            DiscoveryProviderQuotaWindow.window_key == demand.window_key,
        )
        .with_for_update()
    )
    if row is None:
        raise RuntimeError("provider quota window was not persisted")
    if row.limit_units != demand.limit_units:
        row.limit_units = demand.limit_units
    return row


def reserve_provider_page(
    poll_attempt_id: uuid.UUID,
    *,
    adapter_key: str,
    adapter_config: dict[str, Any],
    cursor: dict[str, Any],
    now: datetime | None = None,
) -> PageQuotaDecision:
    current = _utc(now)
    page_key = page_identity(cursor)
    demands = provider_page_demands(adapter_key, adapter_config, now=current)
    if not demands:
        return PageQuotaDecision(True, page_key, "unmetered")

    with session_scope() as session:
        attempt = session.scalar(
            select(TrendWatchPollAttempt)
            .where(TrendWatchPollAttempt.id == poll_attempt_id)
            .with_for_update()
        )
        if attempt is None:
            raise ValueError(f"poll attempt not found: {poll_attempt_id}")
        if attempt.outcome in _TERMINAL_ATTEMPT_OUTCOMES:
            return PageQuotaDecision(False, page_key, attempt.outcome)

        existing = list(
            session.scalars(
                select(DiscoveryQuotaReservation).where(
                    DiscoveryQuotaReservation.poll_attempt_id == poll_attempt_id,
                    DiscoveryQuotaReservation.page_key == page_key,
                )
            )
        )
        if existing:
            if any(item.status == "reserved" for item in existing):
                return PageQuotaDecision(True, page_key, "reused_reservation")
            return PageQuotaDecision(False, page_key, "page_already_settled")

        windows: list[tuple[QuotaDemand, DiscoveryProviderQuotaWindow]] = []
        for demand in demands:
            window = _ensure_quota_window(session, demand)
            blocked_until = _utc(window.blocked_until) if window.blocked_until else None
            if blocked_until is not None and blocked_until > current:
                return PageQuotaDecision(
                    False, page_key, "provider_backoff", blocked_until
                )
            available = int(window.limit_units) - int(window.used_units) - int(
                window.reserved_units
            )
            if available < demand.units:
                return PageQuotaDecision(
                    False, page_key, "provider_quota_exhausted", window.window_end
                )
            windows.append((demand, window))

        for demand, window in windows:
            window.reserved_units += demand.units
            session.add(
                DiscoveryQuotaReservation(
                    poll_attempt_id=poll_attempt_id,
                    quota_window_id=window.id,
                    page_key=page_key,
                    provider_key=demand.provider_key,
                    bucket_key=demand.bucket_key,
                    window_key=demand.window_key,
                    reserved_units=demand.units,
                    consumed_units=0,
                    status="reserved",
                )
            )
        return PageQuotaDecision(True, page_key, "reserved")


def mark_poll_provider_started(poll_attempt_id: uuid.UUID) -> None:
    with session_scope() as session:
        attempt = session.scalar(
            select(TrendWatchPollAttempt)
            .where(TrendWatchPollAttempt.id == poll_attempt_id)
            .with_for_update()
        )
        if attempt is None:
            raise ValueError(f"poll attempt not found: {poll_attempt_id}")
        if attempt.source_quota_reserved:
            attempt.source_quota_consumed += int(attempt.source_quota_reserved)
            attempt.source_quota_reserved = 0
        if attempt.outcome == "reserved":
            attempt.outcome = "running"


def _settle_page_reservations(
    session: Session,
    attempt: TrendWatchPollAttempt,
    page_key: str,
    *,
    provider_usage: dict[str, int],
    consume_reserved_on_missing: bool,
    now: datetime,
) -> None:
    reservations = list(
        session.scalars(
            select(DiscoveryQuotaReservation)
            .where(
                DiscoveryQuotaReservation.poll_attempt_id == attempt.id,
                DiscoveryQuotaReservation.page_key == page_key,
                DiscoveryQuotaReservation.status == "reserved",
            )
            .with_for_update()
        )
    )
    usage = {str(key): max(int(value), 0) for key, value in provider_usage.items()}
    aggregate = dict(attempt.provider_usage or {})
    for key, value in usage.items():
        aggregate[key] = int(aggregate.get(key, 0)) + value
    attempt.provider_usage = aggregate

    for reservation in reservations:
        window = session.scalar(
            select(DiscoveryProviderQuotaWindow)
            .where(DiscoveryProviderQuotaWindow.id == reservation.quota_window_id)
            .with_for_update()
        )
        if window is None:
            continue
        requested = usage.get(reservation.bucket_key)
        if requested is None and consume_reserved_on_missing:
            consumed = int(reservation.reserved_units)
        else:
            consumed = min(max(int(requested or 0), 0), int(reservation.reserved_units))
        window.reserved_units = max(
            0, int(window.reserved_units) - int(reservation.reserved_units)
        )
        window.used_units += consumed
        reservation.consumed_units = consumed
        reservation.status = "settled" if consumed else "released"
        reservation.settled_at = now


def record_poll_page_success(
    poll_attempt_id: uuid.UUID,
    *,
    discovery_run_id: uuid.UUID,
    candidate_count: int,
    next_cursor: dict[str, Any],
    done: bool,
    provider_usage: dict[str, int],
    page_key: str,
    now: datetime | None = None,
) -> str:
    current = _utc(now)
    with session_scope() as session:
        attempt = session.scalar(
            select(TrendWatchPollAttempt)
            .where(TrendWatchPollAttempt.id == poll_attempt_id)
            .with_for_update()
        )
        if attempt is None:
            raise ValueError(f"poll attempt not found: {poll_attempt_id}")
        if attempt.outcome in _TERMINAL_ATTEMPT_OUTCOMES:
            return attempt.outcome
        state = session.scalar(
            select(TrendWatchSourceState)
            .where(TrendWatchSourceState.id == attempt.source_state_id)
            .with_for_update()
        )
        run = session.scalar(
            select(DiscoveryRun)
            .where(DiscoveryRun.id == discovery_run_id)
            .with_for_update()
        )
        if state is None or run is None:
            raise RuntimeError("poll attempt lineage is incomplete")

        _settle_page_reservations(
            session,
            attempt,
            page_key,
            provider_usage=provider_usage,
            consume_reserved_on_missing=False,
            now=current,
        )
        attempt.candidate_count += max(int(candidate_count), 0)
        attempt.pages += 1
        attempt.cursor_after = dict(next_cursor or {})
        prior_health = state.health_status
        state.cursor = dict(next_cursor or {})
        state.last_poll_at = current
        run.cursor = dict(next_cursor or {})
        if attempt.collection_claim_id is not None:
            active_claim = session.get(
                DiscoveryCollectionClaim, attempt.collection_claim_id
            )
            if active_claim is not None and not done:
                active_claim.lease_expires_at = current + timedelta(
                    seconds=get_settings().trend_poll_lease_seconds
                )
        run.status = (
            DiscoveryRunStatus.COMPLETED.value if done else DiscoveryRunStatus.RUNNING.value
        )
        session.add(
            DomainEvent(
                aggregate_type="discovery_run",
                aggregate_id=str(run.id),
                event_type=(
                    "discovery_run.completed"
                    if done
                    else "discovery_run.page_completed"
                ),
                payload={
                    "discovery_run_id": str(run.id),
                    "candidate_count": max(int(candidate_count), 0),
                    "done": done,
                    "poll_attempt_id": str(attempt.id),
                    "provider_usage": dict(provider_usage or {}),
                },
            )
        )
        if done:
            run.completed_at = current
            outcome = "success" if attempt.candidate_count > 0 else "empty_success"
            attempt.outcome = outcome
            attempt.completed_at = current
            state.health_status = "healthy"
            state.consecutive_failures = 0
            state.last_discovery_run_id = run.id
            state.last_success_at = current
            state.backoff_until = None
            state.rate_limit_reset_at = None
            state.last_error_kind = None
            state.last_error_summary = None
            if attempt.collection_claim_id is not None:
                claim = session.get(DiscoveryCollectionClaim, attempt.collection_claim_id)
                if claim is not None:
                    claim.status = "completed"
                    claim.discovery_run_id = run.id
                    claim.completed_at = current
                    claim.blocked_until = None
            session.add(
                DomainEvent(
                    aggregate_type="topic_watch_source",
                    aggregate_id=str(state.id),
                    event_type="topic_watch.poll_succeeded",
                    payload={
                        "source_state_id": str(state.id),
                        "poll_attempt_id": str(attempt.id),
                        "discovery_run_id": str(run.id),
                        "outcome": outcome,
                        "candidate_count": attempt.candidate_count,
                        "pages": attempt.pages,
                        "provider_usage": dict(attempt.provider_usage or {}),
                    },
                )
            )
            if prior_health in {"degraded", "rate_limited", "down"}:
                session.add(
                    DomainEvent(
                        aggregate_type="topic_watch_source",
                        aggregate_id=str(state.id),
                        event_type="topic_watch.source_recovered",
                        payload={
                            "source_state_id": str(state.id),
                            "poll_attempt_id": str(attempt.id),
                            "previous_health": prior_health,
                            "outcome": outcome,
                        },
                    )
                )
            return outcome
        return "running"


def record_poll_failure(
    poll_attempt_id: uuid.UUID,
    *,
    discovery_run_id: uuid.UUID,
    page_key: str,
    kind: str,
    transient: bool,
    status_code: int | None = None,
    retry_after_seconds: int | None = None,
    message: str | None = None,
    consume_reserved: bool = True,
    provider_usage: dict[str, int] | None = None,
    now: datetime | None = None,
) -> str:
    current = _utc(now)
    with session_scope() as session:
        attempt = session.scalar(
            select(TrendWatchPollAttempt)
            .where(TrendWatchPollAttempt.id == poll_attempt_id)
            .with_for_update()
        )
        if attempt is None:
            raise ValueError(f"poll attempt not found: {poll_attempt_id}")
        if attempt.outcome in _TERMINAL_ATTEMPT_OUTCOMES:
            return attempt.outcome
        state = session.scalar(
            select(TrendWatchSourceState)
            .where(TrendWatchSourceState.id == attempt.source_state_id)
            .with_for_update()
        )
        run = session.scalar(
            select(DiscoveryRun)
            .where(DiscoveryRun.id == discovery_run_id)
            .with_for_update()
        )
        if state is None or run is None:
            raise RuntimeError("poll failure lineage is incomplete")

        _settle_page_reservations(
            session,
            attempt,
            page_key,
            provider_usage=dict(provider_usage or {}),
            consume_reserved_on_missing=consume_reserved and provider_usage is None,
            now=current,
        )
        if not consume_reserved and attempt.source_quota_consumed:
            attempt.source_quota_reserved += attempt.source_quota_consumed
            attempt.source_quota_consumed = 0
        if attempt.source_quota_reserved:
            attempt.source_quota_reserved = 0

        state.consecutive_failures = int(state.consecutive_failures or 0) + 1
        delay = deterministic_backoff_seconds(
            attempt.source_identity,
            state.consecutive_failures,
            retry_after_seconds=retry_after_seconds,
        )
        blocked_until = current + timedelta(seconds=delay)
        outcome = (
            "rate_limited"
            if kind == "rate_limited"
            else "transient_failure"
            if transient
            else "permanent_failure"
        )
        state.health_status = (
            "rate_limited"
            if outcome == "rate_limited"
            else "degraded"
            if transient and state.consecutive_failures < 3
            else "down"
        )
        state.last_discovery_run_id = run.id
        state.last_failure_at = current
        state.last_poll_at = current
        state.backoff_until = blocked_until
        state.rate_limit_reset_at = (
            blocked_until if outcome == "rate_limited" else None
        )
        state.last_error_kind = kind
        state.last_error_summary = (message or kind)[:1000]

        attempt.outcome = outcome
        attempt.http_status = status_code
        attempt.retry_after_seconds = retry_after_seconds
        attempt.error_kind = kind
        attempt.completed_at = current
        run.status = DiscoveryRunStatus.FAILED.value
        run.error = (message or kind)[:8000]
        run.completed_at = current

        if attempt.collection_claim_id is not None:
            claim = session.get(DiscoveryCollectionClaim, attempt.collection_claim_id)
            if claim is not None:
                claim.status = "failed"
                claim.blocked_until = blocked_until
                claim.completed_at = current

        if outcome == "rate_limited":
            reservations = list(
                session.scalars(
                    select(DiscoveryQuotaReservation).where(
                        DiscoveryQuotaReservation.poll_attempt_id == attempt.id,
                        DiscoveryQuotaReservation.page_key == page_key,
                    )
                )
            )
            for reservation in reservations:
                window = session.get(
                    DiscoveryProviderQuotaWindow, reservation.quota_window_id
                )
                if window is not None:
                    window.blocked_until = blocked_until

        session.add(
            DomainEvent(
                aggregate_type="topic_watch_source",
                aggregate_id=str(state.id),
                event_type=(
                    "topic_watch.source_rate_limited"
                    if outcome == "rate_limited"
                    else "topic_watch.poll_failed"
                ),
                payload={
                    "source_state_id": str(state.id),
                    "poll_attempt_id": str(attempt.id),
                    "discovery_run_id": str(run.id),
                    "outcome": outcome,
                    "error_kind": kind,
                    "http_status": status_code,
                    "retry_after_seconds": retry_after_seconds,
                    "consecutive_failures": state.consecutive_failures,
                    "next_eligible_poll_at": blocked_until.isoformat(),
                },
            )
        )
        return outcome


def mark_quota_exhausted(
    poll_attempt_id: uuid.UUID,
    *,
    discovery_run_id: uuid.UUID,
    page_key: str,
    reason: str,
    blocked_until: datetime | None,
    now: datetime | None = None,
) -> None:
    current = _utc(now)
    with session_scope() as session:
        attempt = session.scalar(
            select(TrendWatchPollAttempt)
            .where(TrendWatchPollAttempt.id == poll_attempt_id)
            .with_for_update()
        )
        if attempt is None:
            return
        if attempt.outcome in _TERMINAL_ATTEMPT_OUTCOMES:
            return
        attempt.outcome = "quota_exhausted"
        attempt.completed_at = current
        attempt.source_quota_reserved = 0
        attempt.attempt_metadata = {
            **dict(attempt.attempt_metadata or {}),
            "quota_reason": reason,
            "quota_blocked_until": blocked_until.isoformat()
            if blocked_until
            else None,
        }
        run = session.get(DiscoveryRun, discovery_run_id)
        if run is not None:
            run.status = DiscoveryRunStatus.FAILED.value
            run.error = f"discovery quota unavailable: {reason}"
            run.completed_at = current
        state = session.get(TrendWatchSourceState, attempt.source_state_id)
        if state is not None:
            state.last_poll_at = current
            state.health_status = "quota_exhausted"
            state.last_error_kind = reason
            state.last_error_summary = f"discovery quota unavailable: {reason}"
            if blocked_until is not None:
                state.backoff_until = blocked_until
        if attempt.collection_claim_id is not None:
            claim = session.get(DiscoveryCollectionClaim, attempt.collection_claim_id)
            if claim is not None:
                claim.status = "failed"
                claim.blocked_until = blocked_until
                claim.completed_at = current
        session.add(
            DomainEvent(
                aggregate_type="topic_watch_source",
                aggregate_id=str(attempt.source_state_id),
                event_type="topic_watch.quota_exhausted",
                payload={
                    "source_state_id": str(attempt.source_state_id),
                    "poll_attempt_id": str(attempt.id),
                    "reason": reason,
                    "blocked_until": blocked_until.isoformat()
                    if blocked_until
                    else None,
                    "page_key": page_key,
                },
            )
        )


def poll_attempt_for_run(discovery_run_id: uuid.UUID) -> TrendWatchPollAttempt | None:
    with session_scope() as session:
        row = session.scalar(
            select(TrendWatchPollAttempt).where(
                TrendWatchPollAttempt.discovery_run_id == discovery_run_id
            )
        )
        if row is not None:
            session.expunge(row)
        return row


def list_poll_attempts(
    topic_watch_id: uuid.UUID,
    *,
    adapter_index: int | None = None,
    limit: int = 100,
) -> list[TrendWatchPollAttempt]:
    bounded = max(1, min(int(limit), 500))
    with session_scope() as session:
        stmt = (
            select(TrendWatchPollAttempt)
            .join(
                TrendWatchSourceState,
                TrendWatchPollAttempt.source_state_id == TrendWatchSourceState.id,
            )
            .where(TrendWatchSourceState.topic_watch_id == topic_watch_id)
            .order_by(TrendWatchPollAttempt.started_at.desc())
            .limit(bounded)
        )
        if adapter_index is not None:
            stmt = stmt.where(TrendWatchSourceState.adapter_index == adapter_index)
        rows = list(session.scalars(stmt))
        for row in rows:
            session.expunge(row)
        return rows


def source_polling_status(
    topic_watch_id: uuid.UUID,
    adapter_index: int,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    current = _utc(now)
    with session_scope() as session:
        state = session.scalar(
            select(TrendWatchSourceState).where(
                TrendWatchSourceState.topic_watch_id == topic_watch_id,
                TrendWatchSourceState.adapter_index == adapter_index,
            )
        )
        if state is None:
            raise ValueError("topic watch source state not found")
        source_used = _attempt_source_usage(session, state, current)
        provider_key = {
            "youtube": "youtube",
            "reddit": "reddit",
            "rss_atom": "rss_atom",
        }.get(state.adapter_key, state.adapter_key)
        windows = list(
            session.scalars(
                select(DiscoveryProviderQuotaWindow)
                .where(
                    DiscoveryProviderQuotaWindow.provider_key == provider_key,
                    DiscoveryProviderQuotaWindow.window_start <= current,
                    DiscoveryProviderQuotaWindow.window_end > current,
                )
                .order_by(DiscoveryProviderQuotaWindow.bucket_key)
            )
        )
        return {
            "topic_watch_id": str(topic_watch_id),
            "adapter_index": adapter_index,
            "source_state_id": str(state.id),
            "source_identity": state.source_identity,
            "source_quota_limit_per_day": state.source_quota_limit_per_day,
            "source_quota_used": source_used,
            "provider_quota_windows": [
                {
                    "provider_key": row.provider_key,
                    "bucket_key": row.bucket_key,
                    "window_key": row.window_key,
                    "window_start": row.window_start.isoformat(),
                    "window_end": row.window_end.isoformat(),
                    "limit_units": row.limit_units,
                    "used_units": row.used_units,
                    "reserved_units": row.reserved_units,
                    "remaining_units": max(
                        0,
                        int(row.limit_units)
                        - int(row.used_units)
                        - int(row.reserved_units),
                    ),
                    "blocked_until": (
                        row.blocked_until.isoformat() if row.blocked_until else None
                    ),
                }
                for row in windows
            ],
        }


def reset_source_polling(
    topic_watch_id: uuid.UUID,
    adapter_index: int,
    *,
    actor: str,
    reset_source_quota: bool = False,
    reset_provider_quota: bool = False,
    acknowledge_provider_quota_reset: bool = False,
) -> TrendWatchSourceState:
    if reset_provider_quota and not acknowledge_provider_quota_reset:
        raise ValueError(
            "reset_provider_quota requires acknowledge_provider_quota_reset=true"
        )
    current = datetime.now(UTC)
    with session_scope() as session:
        state = session.scalar(
            select(TrendWatchSourceState)
            .where(
                TrendWatchSourceState.topic_watch_id == topic_watch_id,
                TrendWatchSourceState.adapter_index == adapter_index,
            )
            .with_for_update()
        )
        if state is None:
            raise ValueError("topic watch source state not found")
        state.health_status = "unknown"
        state.consecutive_failures = 0
        state.backoff_until = None
        state.rate_limit_reset_at = None
        state.last_error_kind = None
        state.last_error_summary = None

        if reset_source_quota:
            state.state_metadata = {
                **dict(state.state_metadata or {}),
                "source_quota_reset_at": current.isoformat(),
                "source_quota_reset_actor": actor,
            }

        if reset_provider_quota:
            provider_key = {
                "youtube": "youtube",
                "reddit": "reddit",
                "rss_atom": "rss_atom",
            }.get(state.adapter_key, state.adapter_key)
            windows = list(
                session.scalars(
                    select(DiscoveryProviderQuotaWindow).where(
                        DiscoveryProviderQuotaWindow.provider_key == provider_key,
                        DiscoveryProviderQuotaWindow.window_end > current,
                    )
                )
            )
            if any(int(window.reserved_units) > 0 for window in windows):
                raise ValueError(
                    "cannot reset provider quota while reservations are active"
                )
            for window in windows:
                window.used_units = 0
                window.reserved_units = 0
                window.blocked_until = None
                window.quota_metadata = {
                    **dict(window.quota_metadata or {}),
                    "reset_actor": actor,
                    "reset_at": current.isoformat(),
                }

        session.add(
            DomainEvent(
                aggregate_type="topic_watch_source",
                aggregate_id=str(state.id),
                event_type="topic_watch.source_reset",
                payload={
                    "topic_watch_id": str(topic_watch_id),
                    "adapter_index": adapter_index,
                    "actor": actor,
                    "reset_source_quota": reset_source_quota,
                    "reset_provider_quota": reset_provider_quota,
                },
            )
        )
        session.flush()
        session.refresh(state)
        session.expunge(state)
        return state
