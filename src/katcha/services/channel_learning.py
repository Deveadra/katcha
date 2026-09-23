from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from katcha.db import session_scope
from katcha.intelligence.features import snapshot_learning_features
from katcha.intelligence.learning import (
    FEATURE_NAMES,
    TrainingResult,
    TrainingRow,
    blended_score,
    clamp,
    outcome_score,
    train_ranking,
)
from katcha.intelligence_models import (
    ChannelProfile,
    PerformanceObservation,
    RankingSnapshot,
)
from katcha.longform_models import CompilationSegment
from katcha.models import DomainEvent
from katcha.production_models import Production
from katcha.publishing_models import Publication, PublicationAnalyticsSnapshot
from katcha.services.channel_profiles import ensure_active_profile
from katcha.short_episode_models import ShortEpisode, ShortEpisodeItem


def _decimal(value: Decimal | float | int | str) -> Decimal:
    return Decimal(str(value))


def _score01(value: object) -> float:
    try:
        return clamp(float(value or 0) / 100.0)
    except (TypeError, ValueError):
        return 0.0


def _production_features(
    session: Session,
    production_id: uuid.UUID,
) -> dict[str, float]:
    production = session.get(Production, production_id)
    if production is None:
        raise RuntimeError(f"publication production disappeared: {production_id}")
    snapshot = dict(production.analysis_snapshot or {})
    if not snapshot:
        raise RuntimeError("production has no frozen analysis snapshot for learning")
    return snapshot_learning_features(snapshot)


def _compilation_features(
    session: Session,
    compilation_id: uuid.UUID,
) -> dict[str, float]:
    segments = list(
        session.scalars(
            select(CompilationSegment)
            .where(CompilationSegment.compilation_id == compilation_id)
            .order_by(CompilationSegment.position)
        )
    )
    if not segments:
        raise RuntimeError("compilation has no segments available for learning")

    def average(values: list[float]) -> float:
        return sum(values) / len(values) if values else 0.0

    evidence = [dict(segment.evidence or {}) for segment in segments]
    return {
        "baseline_score": clamp(
            average([float(segment.deterministic_score) for segment in segments])
        ),
        "hook_score": clamp(
            average([_score01(item.get("hook_score")) for item in evidence])
        ),
        "surprise_score": clamp(
            average([_score01(item.get("surprise_score")) for item in evidence])
        ),
        "humor_score": clamp(
            average([_score01(item.get("payoff_score")) for item in evidence])
        ),
        "comment_potential": clamp(
            average([_score01(item.get("comment_potential")) for item in evidence])
        ),
        "rewatch_potential": clamp(
            average([_score01(item.get("rewatch_score")) for item in evidence])
        ),
        "duration_signal": clamp(
            average(
                [float(segment.source_duration_seconds) for segment in segments]
            )
            / 60.0
        ),
    }


def _short_episode_features(
    session: Session,
    short_episode_id: uuid.UUID,
) -> dict[str, float]:
    episode = session.get(ShortEpisode, short_episode_id)
    if episode is None:
        raise RuntimeError(f"publication short episode disappeared: {short_episode_id}")
    items = list(
        session.scalars(
            select(ShortEpisodeItem)
            .where(ShortEpisodeItem.short_episode_id == short_episode_id)
            .order_by(ShortEpisodeItem.position)
        )
    )
    if not items:
        raise RuntimeError("short episode has no items available for learning")
    snapshots = [
        snapshot_learning_features(dict(item.analysis_snapshot or {}))
        for item in items
    ]
    return {
        name: clamp(
            sum(float(snapshot.get(name, 0.0)) for snapshot in snapshots)
            / len(snapshots)
        )
        for name in FEATURE_NAMES
    }


def _publication_source_features(
    session: Session,
    publication: Publication,
) -> tuple[str, uuid.UUID, dict[str, float]]:
    if publication.production_id is not None and publication.compilation_id is None:
        return (
            "production",
            publication.production_id,
            _production_features(session, publication.production_id),
        )
    if publication.compilation_id is not None and publication.production_id is None:
        return (
            "compilation",
            publication.compilation_id,
            _compilation_features(session, publication.compilation_id),
        )
    if (
        publication.short_episode_id is not None
        and publication.production_id is None
        and publication.compilation_id is None
    ):
        return (
            "short_episode",
            publication.short_episode_id,
            _short_episode_features(session, publication.short_episode_id),
        )
    raise RuntimeError("publication source lineage is invalid")


def derive_performance_observations(channel_profile_id: uuid.UUID) -> int:
    created = 0
    with session_scope() as session:
        profile = ensure_active_profile(session, channel_profile_id)
        publications = list(
            session.scalars(
                select(Publication).where(
                    Publication.youtube_connection_id
                    == profile.youtube_connection_id
                )
            )
        )
        if not publications:
            return 0
        publication_by_id = {item.id: item for item in publications}
        snapshots = list(
            session.scalars(
                select(PublicationAnalyticsSnapshot)
                .where(
                    PublicationAnalyticsSnapshot.publication_id.in_(
                        publication_by_id
                    )
                )
                .order_by(PublicationAnalyticsSnapshot.sampled_at.asc())
            )
        )
        snapshot_ids = [item.id for item in snapshots]
        existing_snapshot_ids = (
            set(
                session.scalars(
                    select(PerformanceObservation.analytics_snapshot_id).where(
                        PerformanceObservation.analytics_snapshot_id.in_(
                            snapshot_ids
                        )
                    )
                )
            )
            if snapshot_ids
            else set()
        )

        for snapshot in snapshots:
            if snapshot.id in existing_snapshot_ids:
                continue
            publication = publication_by_id[snapshot.publication_id]
            anchor = (
                publication.published_at
                or publication.publish_at
                or publication.created_at
            )
            age_hours = max(
                1.0,
                (snapshot.sampled_at - anchor).total_seconds() / 3600.0,
            )
            source_kind, source_id, features = _publication_source_features(
                session, publication
            )
            views = int(snapshot.views or 0)
            engaged_views = int(snapshot.engaged_views or 0)
            score, signals = outcome_score(
                views=views,
                engaged_views=engaged_views,
                age_hours=age_hours,
                average_view_percentage=float(
                    snapshot.average_view_percentage or 0
                ),
                shares=int(snapshot.shares or 0),
                comments=int(snapshot.comments or 0),
                subscribers_gained=int(snapshot.subscribers_gained or 0),
            )
            labels: dict[str, object] = {
                **signals,
                "views": views,
                "engaged_views": engaged_views,
                "average_view_percentage": float(
                    snapshot.average_view_percentage or 0
                ),
                "shares": int(snapshot.shares or 0),
                "comments": int(snapshot.comments or 0),
                "subscribers_gained": int(snapshot.subscribers_gained or 0),
                "estimated_revenue": (
                    str(snapshot.estimated_revenue)
                    if snapshot.estimated_revenue is not None
                    else None
                ),
            }
            session.add(
                PerformanceObservation(
                    channel_profile_id=profile.id,
                    publication_id=publication.id,
                    analytics_snapshot_id=snapshot.id,
                    source_kind=source_kind,
                    source_id=source_id,
                    observed_at=snapshot.sampled_at,
                    publication_age_hours=_decimal(round(age_hours, 4)),
                    features=features,
                    labels=labels,
                    outcome_score=_decimal(score),
                )
            )
            created += 1

        if created:
            session.add(
                DomainEvent(
                    aggregate_type="channel_profile",
                    aggregate_id=str(profile.id),
                    event_type="channel_profile.observations_refreshed",
                    payload={
                        "channel_profile_id": str(profile.id),
                        "created_observations": created,
                    },
                )
            )
    return created


def latest_observations(
    session: Session,
    profile_id: uuid.UUID,
) -> list[PerformanceObservation]:
    rows = list(
        session.scalars(
            select(PerformanceObservation)
            .where(PerformanceObservation.channel_profile_id == profile_id)
            .order_by(PerformanceObservation.observed_at.asc())
        )
    )
    latest: dict[uuid.UUID, PerformanceObservation] = {}
    for row in rows:
        latest[row.publication_id] = row
    return sorted(latest.values(), key=lambda item: item.observed_at)


def train_channel_ranking(
    channel_profile_id: uuid.UUID,
    *,
    run_key: str | None = None,
) -> RankingSnapshot:
    key = run_key or f"manual-{uuid.uuid4().hex}"
    with session_scope() as session:
        profile = ensure_active_profile(session, channel_profile_id)
        existing = session.scalar(
            select(RankingSnapshot).where(
                RankingSnapshot.channel_profile_id == profile.id,
                RankingSnapshot.run_key == key,
            )
        )
        if existing is not None:
            session.expunge(existing)
            return existing
        observations = latest_observations(session, profile.id)
        rows = [
            TrainingRow(
                features={
                    name: float((item.features or {}).get(name, 0))
                    for name in FEATURE_NAMES
                },
                outcome=float(item.outcome_score),
            )
            for item in observations
        ]
        result = train_ranking(rows)
        version = int(
            session.scalar(
                select(func.coalesce(func.max(RankingSnapshot.version), 0)).where(
                    RankingSnapshot.channel_profile_id == profile.id
                )
            )
            or 0
        ) + 1
        snapshot = RankingSnapshot(
            channel_profile_id=profile.id,
            version=version,
            run_key=key,
            algorithm=result.algorithm,
            training_cutoff=datetime.now(UTC),
            sample_count=result.sample_count,
            feature_names=list(result.feature_names),
            feature_means=result.feature_means,
            feature_scales=result.feature_scales,
            coefficients=result.coefficients,
            intercept=_decimal(result.intercept),
            blend_ratio=_decimal(result.blend_ratio),
            confidence=_decimal(result.confidence),
            validation_metrics=result.validation_metrics,
        )
        session.add(snapshot)
        session.add(
            DomainEvent(
                aggregate_type="channel_profile",
                aggregate_id=str(profile.id),
                event_type="channel_profile.ranking_trained",
                payload={
                    "channel_profile_id": str(profile.id),
                    "ranking_version": version,
                    "run_key": key,
                    "algorithm": result.algorithm,
                    "sample_count": result.sample_count,
                    "confidence": result.confidence,
                    "blend_ratio": result.blend_ratio,
                },
            )
        )
        session.flush()
        session.refresh(snapshot)
        session.expunge(snapshot)
        return snapshot


def score_channel_features(
    channel_profile_id: uuid.UUID,
    features: dict[str, float],
) -> tuple[float, dict[str, float | str]]:
    with session_scope() as session:
        ensure_active_profile(session, channel_profile_id)
        snapshot = session.scalar(
            select(RankingSnapshot)
            .where(RankingSnapshot.channel_profile_id == channel_profile_id)
            .order_by(RankingSnapshot.version.desc())
            .limit(1)
        )
        if snapshot is None:
            baseline = clamp(float(features.get("baseline_score", 0)))
            return baseline, {
                "baseline": baseline,
                "learned": baseline,
                "blend_ratio": 0.0,
                "algorithm": "untrained",
            }
        result = TrainingResult(
            algorithm=snapshot.algorithm,
            sample_count=snapshot.sample_count,
            feature_names=tuple(snapshot.feature_names),
            feature_means={
                key: float(value)
                for key, value in snapshot.feature_means.items()
            },
            feature_scales={
                key: float(value)
                for key, value in snapshot.feature_scales.items()
            },
            coefficients={
                key: float(value)
                for key, value in snapshot.coefficients.items()
            },
            intercept=float(snapshot.intercept),
            confidence=float(snapshot.confidence),
            blend_ratio=float(snapshot.blend_ratio),
            validation_metrics=dict(snapshot.validation_metrics or {}),
        )
        return blended_score(features, result)


def latest_ranking_snapshot(
    session: Session,
    profile: ChannelProfile,
) -> RankingSnapshot | None:
    return session.scalar(
        select(RankingSnapshot)
        .where(RankingSnapshot.channel_profile_id == profile.id)
        .order_by(RankingSnapshot.version.desc())
        .limit(1)
    )
