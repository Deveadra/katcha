from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select

from katcha.config import get_settings
from katcha.db import session_scope
from katcha.intelligence_models import ChannelProfile
from katcha.models import DomainEvent
from katcha.services.trends import register_signal
from katcha.trend_source_models import TrendSourcePoll, TrendSourceSubscription
from katcha.trends.adapters import (
    TrendAdapterContext,
    TrendAdapterError,
    available_trend_adapters,
    get_trend_adapter,
)

_FINAL_POLL_STATUSES = {"succeeded", "failed", "skipped"}
_SECRET_FRAGMENTS = (
    "access_token",
    "refresh_token",
    "api_key",
    "apikey",
    "client_secret",
    "password",
    "authorization",
    "credential",
)


def _utc(value: datetime | None = None) -> datetime:
    value = value or datetime.now(UTC)
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _assert_no_secrets(value: object, path: str = "query") -> None:
    if isinstance(value, dict):
        for raw_key, child in value.items():
            key = str(raw_key).casefold().replace("-", "_")
            if any(fragment in key for fragment in _SECRET_FRAGMENTS):
                raise ValueError(f"secret-bearing field is not allowed in {path}: {raw_key}")
            _assert_no_secrets(child, f"{path}.{raw_key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _assert_no_secrets(child, f"{path}[{index}]")


def _canonical_json(value: dict[str, Any]) -> str:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    except (TypeError, ValueError) as exc:
        raise ValueError("trend source query must be JSON serializable") from exc


def _subscription_key(adapter_key: str, adapter_version: str, query: dict[str, Any]) -> str:
    raw = f"{adapter_key}\x1f{adapter_version}\x1f{_canonical_json(query)}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _poll_payload(poll: TrendSourcePoll) -> dict[str, Any]:
    return {
        "poll_id": str(poll.id),
        "trend_source_subscription_id": str(poll.trend_source_subscription_id),
        "run_key": poll.run_key,
        "status": poll.status,
        "item_count": poll.item_count,
        "error_kind": poll.error_kind,
        "retry_after_seconds": poll.retry_after_seconds,
        "poll_metadata": dict(poll.poll_metadata),
    }


def available_source_adapters() -> list[dict[str, str]]:
    return available_trend_adapters()


def create_trend_source_subscription(
    channel_profile_id: uuid.UUID,
    *,
    name: str,
    adapter_key: str,
    adapter_version: str = "v1",
    query: dict[str, Any],
    poll_interval_seconds: int = 900,
    metadata: dict[str, Any] | None = None,
    actor: str = "operator",
) -> TrendSourceSubscription:
    adapter_key = adapter_key.strip().casefold()
    adapter_version = adapter_version.strip()
    name = name.strip()
    if not name:
        raise ValueError("trend source name is required")
    if poll_interval_seconds < 60:
        raise ValueError("trend source poll interval must be at least 60 seconds")
    _assert_no_secrets(query)
    _assert_no_secrets(metadata or {}, "metadata")
    get_trend_adapter(adapter_key, adapter_version)
    normalized_query = json.loads(_canonical_json(query))
    key = _subscription_key(adapter_key, adapter_version, normalized_query)

    with session_scope() as session:
        if session.get(ChannelProfile, channel_profile_id) is None:
            raise ValueError(f"channel profile not found: {channel_profile_id}")
        existing = session.scalar(
            select(TrendSourceSubscription).where(
                TrendSourceSubscription.channel_profile_id == channel_profile_id,
                TrendSourceSubscription.subscription_key == key,
            )
        )
        if existing is not None:
            return existing
        source = TrendSourceSubscription(
            channel_profile_id=channel_profile_id,
            subscription_key=key,
            name=name,
            adapter_key=adapter_key,
            adapter_version=adapter_version,
            status="active",
            source_query=normalized_query,
            cursor={},
            poll_interval_seconds=poll_interval_seconds,
            health_status="unknown",
            consecutive_failures=0,
            next_poll_at=datetime.now(UTC),
            source_metadata=metadata or {},
        )
        session.add(source)
        session.flush()
        session.add(
            DomainEvent(
                aggregate_type="trend_source",
                aggregate_id=str(source.id),
                event_type="trend.source.created",
                payload={
                    "trend_source_subscription_id": str(source.id),
                    "channel_profile_id": str(channel_profile_id),
                    "adapter_key": adapter_key,
                    "adapter_version": adapter_version,
                    "poll_interval_seconds": poll_interval_seconds,
                    "actor": actor,
                },
            )
        )
        return source


def list_trend_sources(channel_profile_id: uuid.UUID) -> list[TrendSourceSubscription]:
    with session_scope() as session:
        return list(
            session.scalars(
                select(TrendSourceSubscription)
                .where(TrendSourceSubscription.channel_profile_id == channel_profile_id)
                .order_by(TrendSourceSubscription.created_at)
            )
        )


def get_trend_source(source_id: uuid.UUID) -> TrendSourceSubscription | None:
    with session_scope() as session:
        return session.get(TrendSourceSubscription, source_id)


def set_trend_source_status(
    source_id: uuid.UUID,
    *,
    enabled: bool,
    actor: str = "operator",
) -> TrendSourceSubscription:
    with session_scope() as session:
        source = session.get(TrendSourceSubscription, source_id)
        if source is None:
            raise ValueError(f"trend source not found: {source_id}")
        source.status = "active" if enabled else "paused"
        if enabled:
            source.next_poll_at = datetime.now(UTC)
        session.add(
            DomainEvent(
                aggregate_type="trend_source",
                aggregate_id=str(source.id),
                event_type="trend.source.status_changed",
                payload={
                    "trend_source_subscription_id": str(source.id),
                    "status": source.status,
                    "actor": actor,
                },
            )
        )
        return source


def source_health_summary(channel_profile_id: uuid.UUID) -> dict[str, Any]:
    sources = list_trend_sources(channel_profile_id)
    active = [source for source in sources if source.status == "active"]
    if not active:
        return {
            "configured": 0,
            "healthy": 0,
            "degraded": 0,
            "coverage": 1.0,
        }
    healthy = sum(1 for source in active if source.health_status == "healthy")
    unknown = sum(1 for source in active if source.health_status == "unknown")
    degraded = len(active) - healthy - unknown
    coverage = (healthy + 0.5 * unknown) / len(active)
    return {
        "configured": len(active),
        "healthy": healthy,
        "degraded": degraded,
        "unknown": unknown,
        "coverage": round(coverage, 6),
    }


def _failure_backoff_seconds(
    source: TrendSourceSubscription,
    *,
    failure_count: int,
    requested_retry_after: int | None,
) -> int:
    settings = get_settings()
    exponent = min(max(0, failure_count - 1), 6)
    computed = source.poll_interval_seconds * (2**exponent)
    retry_after = max(0, int(requested_retry_after or 0))
    return min(
        settings.trend_source_max_backoff_seconds,
        max(60, computed, retry_after),
    )


def poll_trend_source(
    source_id: uuid.UUID,
    *,
    run_key: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    now = _utc(now)
    settings = get_settings()
    with session_scope() as session:
        source = session.get(TrendSourceSubscription, source_id)
        if source is None:
            raise ValueError(f"trend source not found: {source_id}")
        existing = session.scalar(
            select(TrendSourcePoll).where(
                TrendSourcePoll.trend_source_subscription_id == source_id,
                TrendSourcePoll.run_key == run_key,
            )
        )
        if existing is not None and existing.status in _FINAL_POLL_STATUSES:
            return _poll_payload(existing)
        if existing is None:
            existing = TrendSourcePoll(
                trend_source_subscription_id=source_id,
                run_key=run_key,
                status="running",
                item_count=0,
                cursor_before=dict(source.cursor),
                cursor_after={},
                poll_metadata={},
            )
            session.add(existing)
            session.flush()
        if source.status != "active":
            existing.status = "skipped"
            existing.completed_at = now
            existing.poll_metadata = {"reason": "source_paused"}
            return _poll_payload(existing)
        backoff_until = _utc(source.backoff_until) if source.backoff_until else None
        if backoff_until is not None and backoff_until > now:
            retry_in = max(1, int((backoff_until - now).total_seconds()))
            existing.status = "skipped"
            existing.completed_at = now
            existing.retry_after_seconds = retry_in
            existing.poll_metadata = {"reason": "source_backoff"}
            return _poll_payload(existing)
        profile = session.get(ChannelProfile, source.channel_profile_id)
        if profile is None:
            raise ValueError(f"channel profile not found: {source.channel_profile_id}")
        source_query = dict(source.source_query)
        source_cursor = dict(source.cursor)
        adapter_key = source.adapter_key
        adapter_version = source.adapter_version
        youtube_connection_id = profile.youtube_connection_id
        poll_id = existing.id

    adapter = get_trend_adapter(adapter_key, adapter_version)
    context = TrendAdapterContext(
        channel_profile_id=source.channel_profile_id,
        youtube_connection_id=youtube_connection_id,
        observed_at=now,
        settings=settings,
    )
    try:
        batch = adapter.poll(source_query, source_cursor, context)
    except TrendAdapterError as exc:
        with session_scope() as session:
            stored_source = session.get(TrendSourceSubscription, source_id)
            poll = session.get(TrendSourcePoll, poll_id)
            if stored_source is None or poll is None:
                raise RuntimeError("trend source state disappeared during poll") from exc
            failures = stored_source.consecutive_failures + 1
            retry_seconds = _failure_backoff_seconds(
                stored_source,
                failure_count=failures,
                requested_retry_after=exc.retry_after_seconds,
            )
            stored_source.consecutive_failures = failures
            stored_source.last_failure_at = now
            stored_source.backoff_until = now + timedelta(seconds=retry_seconds)
            stored_source.next_poll_at = stored_source.backoff_until
            stored_source.last_error_kind = exc.kind
            stored_source.last_error_summary = str(exc)[:1000]
            if exc.kind == "rate_limited":
                stored_source.health_status = "rate_limited"
            elif not exc.transient or failures >= 3:
                stored_source.health_status = "down"
            else:
                stored_source.health_status = "degraded"
            poll.status = "failed"
            poll.completed_at = now
            poll.error_kind = exc.kind
            poll.retry_after_seconds = retry_seconds
            poll.cursor_after = dict(stored_source.cursor)
            poll.poll_metadata = {
                "adapter_key": adapter_key,
                "adapter_version": adapter_version,
                "transient": exc.transient,
            }
            session.add(
                DomainEvent(
                    aggregate_type="trend_source",
                    aggregate_id=str(source_id),
                    event_type="trend.source.poll_failed",
                    payload={
                        "trend_source_subscription_id": str(source_id),
                        "adapter_key": adapter_key,
                        "health_status": stored_source.health_status,
                        "error_kind": exc.kind,
                        "retry_after_seconds": retry_seconds,
                        "consecutive_failures": failures,
                    },
                )
            )
            return _poll_payload(poll)
    except Exception as exc:
        safe_error = TrendAdapterError(
            "trend adapter raised an unexpected error",
            kind="unexpected_adapter_error",
        )
        with session_scope() as session:
            stored_source = session.get(TrendSourceSubscription, source_id)
            poll = session.get(TrendSourcePoll, poll_id)
            if stored_source is None or poll is None:
                raise RuntimeError("trend source state disappeared during poll") from exc
            failures = stored_source.consecutive_failures + 1
            retry_seconds = _failure_backoff_seconds(
                stored_source,
                failure_count=failures,
                requested_retry_after=None,
            )
            stored_source.consecutive_failures = failures
            stored_source.last_failure_at = now
            stored_source.backoff_until = now + timedelta(seconds=retry_seconds)
            stored_source.next_poll_at = stored_source.backoff_until
            stored_source.last_error_kind = safe_error.kind
            stored_source.last_error_summary = safe_error.args[0]
            stored_source.health_status = "down" if failures >= 3 else "degraded"
            poll.status = "failed"
            poll.completed_at = now
            poll.error_kind = safe_error.kind
            poll.retry_after_seconds = retry_seconds
            poll.cursor_after = dict(stored_source.cursor)
            poll.poll_metadata = {
                "adapter_key": adapter_key,
                "adapter_version": adapter_version,
                "exception_type": type(exc).__name__,
            }
            session.add(
                DomainEvent(
                    aggregate_type="trend_source",
                    aggregate_id=str(source_id),
                    event_type="trend.source.poll_failed",
                    payload={
                        "trend_source_subscription_id": str(source_id),
                        "adapter_key": adapter_key,
                        "health_status": stored_source.health_status,
                        "error_kind": safe_error.kind,
                        "retry_after_seconds": retry_seconds,
                        "consecutive_failures": failures,
                    },
                )
            )
            return _poll_payload(poll)

    emitted = 0
    for observation in batch.observations:
        register_signal(
            topic=observation.topic,
            provider_key=observation.provider_key,
            external_id=observation.external_id,
            source_kind=observation.source_kind,
            independence_key=observation.independence_key,
            observed_at=now,
            canonical_url=observation.canonical_url,
            source_name=observation.source_name,
            title=observation.title,
            body_excerpt=observation.body_excerpt,
            author=observation.author,
            community=observation.community,
            language=observation.language,
            region=observation.region,
            published_at=observation.published_at,
            metrics=observation.metrics,
            media_refs=list(observation.media_refs),
            aliases=list(observation.aliases),
            tags=list(observation.tags),
            content_fingerprint=observation.content_fingerprint,
            metadata={
                **observation.metadata,
                "trend_source_subscription_id": str(source_id),
                "adapter_key": adapter_key,
                "adapter_version": adapter_version,
            },
            match_confidence=observation.match_confidence,
            match_reasons=list(observation.match_reasons),
        )
        emitted += 1

    with session_scope() as session:
        stored_source = session.get(TrendSourceSubscription, source_id)
        poll = session.get(TrendSourcePoll, poll_id)
        if stored_source is None or poll is None:
            raise RuntimeError("trend source state disappeared during poll")
        stored_source.cursor = dict(batch.next_cursor)
        stored_source.health_status = "healthy"
        stored_source.consecutive_failures = 0
        stored_source.last_success_at = now
        stored_source.last_error_kind = None
        stored_source.last_error_summary = None
        stored_source.backoff_until = None
        stored_source.next_poll_at = now + timedelta(
            seconds=stored_source.poll_interval_seconds
        )
        poll.status = "succeeded"
        poll.completed_at = now
        poll.item_count = emitted
        poll.cursor_after = dict(batch.next_cursor)
        poll.error_kind = None
        poll.retry_after_seconds = None
        poll.poll_metadata = {
            "adapter_key": adapter_key,
            "adapter_version": adapter_version,
            **batch.metadata,
        }
        session.add(
            DomainEvent(
                aggregate_type="trend_source",
                aggregate_id=str(source_id),
                event_type="trend.source.poll_succeeded",
                payload={
                    "trend_source_subscription_id": str(source_id),
                    "channel_profile_id": str(stored_source.channel_profile_id),
                    "adapter_key": adapter_key,
                    "item_count": emitted,
                    "health_status": "healthy",
                    "run_key": run_key,
                },
            )
        )
        return _poll_payload(poll)
