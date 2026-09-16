from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from katcha.acquisition.trends import (
    ALGORITHM_VERSION,
    TrendObservation,
    score_trend,
)
from katcha.acquisition_models import (
    CandidateTrendScore,
    DiscoveryCandidate,
    DiscoveryObservation,
    DiscoveryRun,
    TopicWatchVersion,
)
from katcha.db import session_scope
from katcha.models import DomainEvent


def _normalize_terms(values: list[str] | tuple[str, ...]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for raw in values:
        value = str(raw).strip()
        key = value.casefold()
        if not value or key in seen:
            continue
        seen.add(key)
        result.append(value)
    return result


def create_topic_watch_version(
    *,
    watch_key: str,
    name: str,
    include_terms: list[str] | tuple[str, ...],
    exclude_terms: list[str] | tuple[str, ...] = (),
    adapter_configs: list[dict[str, Any]] | None = None,
    language: str | None = None,
    locale: str | None = None,
    freshness_horizon_hours: int = 72,
    max_candidates: int = 100,
    enabled: bool = True,
    metadata: dict[str, Any] | None = None,
) -> TopicWatchVersion:
    key = watch_key.strip()
    display_name = name.strip()
    if not key:
        raise ValueError("watch_key is required")
    if not display_name:
        raise ValueError("topic watch name is required")
    if freshness_horizon_hours <= 0:
        raise ValueError("freshness_horizon_hours must be positive")
    if max_candidates <= 0:
        raise ValueError("max_candidates must be positive")

    with session_scope() as session:
        version = int(
            session.scalar(
                select(func.coalesce(func.max(TopicWatchVersion.version), 0)).where(
                    TopicWatchVersion.watch_key == key
                )
            )
            or 0
        ) + 1
        row = TopicWatchVersion(
            watch_key=key,
            version=version,
            name=display_name,
            enabled=enabled,
            include_terms=_normalize_terms(include_terms),
            exclude_terms=_normalize_terms(exclude_terms),
            adapter_configs=[dict(item) for item in (adapter_configs or [])],
            language=language.strip() if language else None,
            locale=locale.strip() if locale else None,
            freshness_horizon_hours=freshness_horizon_hours,
            max_candidates=max_candidates,
            watch_metadata=dict(metadata or {}),
        )
        session.add(row)
        session.flush()
        session.add(
            DomainEvent(
                aggregate_type="topic_watch",
                aggregate_id=key,
                event_type="topic_watch.version_created",
                payload={
                    "topic_watch_id": str(row.id),
                    "watch_key": key,
                    "version": version,
                    "enabled": enabled,
                    "adapter_count": len(row.adapter_configs),
                    "freshness_horizon_hours": freshness_horizon_hours,
                    "max_candidates": max_candidates,
                },
            )
        )
        session.refresh(row)
        session.expunge(row)
        return row


def latest_topic_watch(session: Session, watch_key: str) -> TopicWatchVersion | None:
    return session.scalar(
        select(TopicWatchVersion)
        .where(TopicWatchVersion.watch_key == watch_key)
        .order_by(TopicWatchVersion.version.desc())
        .limit(1)
    )


def _parse_datetime(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _source_metrics(metadata: dict[str, Any]) -> dict[str, float]:
    raw = metadata.get("source_metrics")
    if not isinstance(raw, dict):
        return {}
    result: dict[str, float] = {}
    for key, value in raw.items():
        try:
            result[str(key)] = float(value)
        except (TypeError, ValueError):
            continue
    return result


def _candidate_observations(
    session: Session,
    candidate_id: uuid.UUID,
) -> list[TrendObservation]:
    rows = list(
        session.execute(
            select(DiscoveryObservation, DiscoveryRun.adapter_key)
            .join(
                DiscoveryRun,
                DiscoveryObservation.discovery_run_id == DiscoveryRun.id,
            )
            .where(DiscoveryObservation.discovery_candidate_id == candidate_id)
            .order_by(DiscoveryObservation.observed_at.asc())
        )
    )
    result: list[TrendObservation] = []
    for observation, adapter_key in rows:
        metadata = dict(observation.observation_metadata or {})
        result.append(
            TrendObservation(
                observed_at=observation.observed_at,
                published_at=_parse_datetime(metadata.get("published_at")),
                source_key=str(adapter_key),
                metrics=_source_metrics(metadata),
            )
        )
    return result


def compute_candidate_trend_score(
    candidate_id: uuid.UUID,
    topic_watch_id: uuid.UUID,
    *,
    related_item_count: int = 0,
    corroborating_source_count: int | None = None,
    now: datetime | None = None,
) -> CandidateTrendScore:
    with session_scope() as session:
        candidate = session.get(DiscoveryCandidate, candidate_id)
        if candidate is None:
            raise ValueError(f"discovery candidate not found: {candidate_id}")
        watch = session.get(TopicWatchVersion, topic_watch_id)
        if watch is None:
            raise ValueError(f"topic watch version not found: {topic_watch_id}")
        observations = _candidate_observations(session, candidate_id)
        result = score_trend(
            observations,
            now=now,
            freshness_horizon_hours=watch.freshness_horizon_hours,
            related_item_count=related_item_count,
            corroborating_source_count=corroborating_source_count,
        )
        version = int(
            session.scalar(
                select(func.coalesce(func.max(CandidateTrendScore.version), 0)).where(
                    CandidateTrendScore.topic_watch_id == topic_watch_id,
                    CandidateTrendScore.discovery_candidate_id == candidate_id,
                )
            )
            or 0
        ) + 1
        row = CandidateTrendScore(
            topic_watch_id=topic_watch_id,
            discovery_candidate_id=candidate_id,
            version=version,
            algorithm_version=ALGORITHM_VERSION,
            score=Decimal(str(result.score)),
            feature_breakdown=result.breakdown(),
            observation_count=result.observation_count,
            window_start=result.window_start,
            window_end=result.window_end,
        )
        session.add(row)
        session.flush()
        session.add(
            DomainEvent(
                aggregate_type="discovery_candidate",
                aggregate_id=str(candidate_id),
                event_type="discovery_candidate.trend_scored",
                payload={
                    "discovery_candidate_id": str(candidate_id),
                    "topic_watch_id": str(topic_watch_id),
                    "watch_key": watch.watch_key,
                    "watch_version": watch.version,
                    "trend_score_id": str(row.id),
                    "trend_score_version": version,
                    "algorithm_version": ALGORITHM_VERSION,
                    "score": result.score,
                    "observation_count": result.observation_count,
                    "source_count": result.source_count,
                },
            )
        )
        session.refresh(row)
        session.expunge(row)
        return row
