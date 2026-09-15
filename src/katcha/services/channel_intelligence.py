from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from katcha.config import get_settings
from katcha.db import session_scope
from katcha.domain import AutomationLevel, ChannelStatus
from katcha.intelligence.learning import (
    FEATURE_NAMES,
    TrainingResult,
    TrainingRow,
    blended_score,
    clamp,
    outcome_score,
    train_ranking,
)
from katcha.intelligence.scheduling import HistoricalSlot, recommend_windows
from katcha.intelligence_models import (
    AutomationPolicyVersion,
    ChannelEconomicsSnapshot,
    ChannelProfile,
    ChannelStrategyVersion,
    PerformanceObservation,
    RankingSnapshot,
    ScheduleRecommendation,
)
from katcha.integrations.youtube.oauth import MONETARY_SCOPE
from katcha.longform_models import CompilationSegment
from katcha.models import Clip, ClipFeature, DomainEvent, UsageEvent
from katcha.production_models import Production
from katcha.publishing_models import (
    Publication,
    PublicationAnalyticsSnapshot,
    YouTubeConnection,
)


def _decimal(value: Decimal | float | int | str) -> Decimal:
    return Decimal(str(value))


def _timezone(value: str) -> str:
    candidate = value.strip()
    if not candidate:
        raise ValueError("channel timezone cannot be empty")
    try:
        ZoneInfo(candidate)
    except ZoneInfoNotFoundError as exc:
        raise ValueError(f"unknown IANA timezone: {candidate}") from exc
    return candidate


def _validate_schedule(items: list[dict[str, object]]) -> list[dict[str, int]]:
    result: list[dict[str, int]] = []
    seen: set[tuple[int, int]] = set()
    for raw in items:
        try:
            weekday = int(raw["weekday"])
            hour_local = int(raw["hour_local"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("schedule entries require integer weekday and hour_local") from exc
        if not 0 <= weekday <= 6 or not 0 <= hour_local <= 23:
            raise ValueError("schedule weekday/hour are outside valid ranges")
        key = (weekday, hour_local)
        if key in seen:
            continue
        seen.add(key)
        result.append({"weekday": weekday, "hour_local": hour_local})
    return result


def active_strategy(session: Session, profile: ChannelProfile) -> ChannelStrategyVersion:
    strategy = session.scalar(
        select(ChannelStrategyVersion).where(
            ChannelStrategyVersion.channel_profile_id == profile.id,
            ChannelStrategyVersion.version == profile.active_strategy_version,
        )
    )
    if strategy is None:
        raise RuntimeError("channel profile has no active strategy version")
    return strategy


def active_automation(session: Session, profile: ChannelProfile) -> AutomationPolicyVersion:
    policy = session.scalar(
        select(AutomationPolicyVersion).where(
            AutomationPolicyVersion.channel_profile_id == profile.id,
            AutomationPolicyVersion.version == profile.active_automation_version,
        )
    )
    if policy is None:
        raise RuntimeError("channel profile has no active automation policy")
    return policy


def ensure_channel_profile(
    youtube_connection_id: uuid.UUID,
    *,
    timezone: str = "UTC",
    fallback_schedule: list[dict[str, object]] | None = None,
) -> ChannelProfile:
    timezone = _timezone(timezone)
    fallback = _validate_schedule(fallback_schedule or [])
    settings = get_settings()
    with session_scope() as session:
        existing = session.scalar(
            select(ChannelProfile).where(
                ChannelProfile.youtube_connection_id == youtube_connection_id
            )
        )
        if existing is not None:
            session.expunge(existing)
            return existing
        connection = session.get(YouTubeConnection, youtube_connection_id)
        if connection is None:
            raise ValueError(f"YouTube connection not found: {youtube_connection_id}")

        profile = ChannelProfile(
            youtube_connection_id=youtube_connection_id,
            status=ChannelStatus.ACTIVE.value,
            timezone=timezone,
            active_strategy_version=1,
            active_automation_version=1,
            profile_metadata={"channel_id": connection.channel_id},
        )
        session.add(profile)
        session.flush()
        session.add(
            ChannelStrategyVersion(
                channel_profile_id=profile.id,
                version=1,
                monthly_hard_budget_usd=_decimal(settings.ai_budget_usd_monthly),
                reinvestment_rate=Decimal("1"),
                reinvestment_cap_usd=Decimal("100"),
                fallback_schedule=fallback,
                blackout_windows=[],
                routing_policy={"mode": "balanced", "quality_floor": "task_default"},
                strategy_metadata={"created_by": "profile_bootstrap"},
            )
        )
        session.add(
            AutomationPolicyVersion(
                channel_profile_id=profile.id,
                version=1,
                level=AutomationLevel.REVIEW_REQUIRED.value,
                policy_metadata={"created_by": "profile_bootstrap"},
            )
        )
        session.add(
            DomainEvent(
                aggregate_type="channel_profile",
                aggregate_id=str(profile.id),
                event_type="channel_profile.created",
                payload={
                    "channel_profile_id": str(profile.id),
                    "youtube_connection_id": str(youtube_connection_id),
                    "timezone": timezone,
                    "automation_level": AutomationLevel.REVIEW_REQUIRED.value,
                },
            )
        )
        session.refresh(profile)
        session.expunge(profile)
        return profile


def create_strategy_version(
    channel_profile_id: uuid.UUID,
    *,
    monthly_hard_budget_usd: Decimal | None = None,
    reinvestment_rate: Decimal | None = None,
    reinvestment_cap_usd: Decimal | None = None,
    fallback_schedule: list[dict[str, object]] | None = None,
    blackout_windows: list[dict[str, object]] | None = None,
    routing_policy: dict[str, object] | None = None,
    actor: str = "operator",
) -> ChannelStrategyVersion:
    with session_scope() as session:
        profile = session.get(ChannelProfile, channel_profile_id)
        if profile is None:
            raise ValueError(f"channel profile not found: {channel_profile_id}")
        current = active_strategy(session, profile)
        budget = (
            monthly_hard_budget_usd
            if monthly_hard_budget_usd is not None
            else current.monthly_hard_budget_usd
        )
        rate = reinvestment_rate if reinvestment_rate is not None else current.reinvestment_rate
        cap = (
            reinvestment_cap_usd
            if reinvestment_cap_usd is not None
            else current.reinvestment_cap_usd
        )
        if budget < 0 or cap < 0:
            raise ValueError("channel budgets cannot be negative")
        if rate < 0 or rate > 1:
            raise ValueError("reinvestment_rate must be between 0 and 1")
        fallback = (
            _validate_schedule(fallback_schedule)
            if fallback_schedule is not None
            else list(current.fallback_schedule or [])
        )
        blackouts = (
            _validate_schedule(blackout_windows)
            if blackout_windows is not None
            else list(current.blackout_windows or [])
        )
        version = profile.active_strategy_version + 1
        strategy = ChannelStrategyVersion(
            channel_profile_id=profile.id,
            version=version,
            monthly_hard_budget_usd=budget,
            reinvestment_rate=rate,
            reinvestment_cap_usd=cap,
            fallback_schedule=fallback,
            blackout_windows=blackouts,
            routing_policy=dict(routing_policy or current.routing_policy or {}),
            strategy_metadata={"actor": actor, "supersedes": current.version},
        )
        session.add(strategy)
        profile.active_strategy_version = version
        session.add(
            DomainEvent(
                aggregate_type="channel_profile",
                aggregate_id=str(profile.id),
                event_type="channel_profile.strategy_updated",
                payload={
                    "channel_profile_id": str(profile.id),
                    "strategy_version": version,
                    "actor": actor,
                },
            )
        )
        session.flush()
        session.refresh(strategy)
        session.expunge(strategy)
        return strategy


def _active_ai(features: ClipFeature) -> dict[str, object]:
    ai = dict(features.ai_features or {})
    deep = ai.get("deep")
    bulk = ai.get("bulk")
    if isinstance(deep, dict):
        return deep
    if isinstance(bulk, dict):
        return bulk
    return {}


def _score01(value: object) -> float:
    try:
        return clamp(float(value or 0) / 100.0)
    except (TypeError, ValueError):
        return 0.0


def _production_features(session: Session, production_id: uuid.UUID) -> dict[str, float]:
    production = session.get(Production, production_id)
    if production is None:
        raise RuntimeError(f"publication production disappeared: {production_id}")
    clip = session.get(Clip, production.clip_id)
    features = session.get(ClipFeature, production.clip_id)
    if clip is None or features is None:
        raise RuntimeError("production clip/features are unavailable for learning")
    ai = _active_ai(features)
    duration = float(clip.duration_seconds or 0)
    return {
        "baseline_score": clamp(float(features.candidate_score or 0) / 100.0),
        "hook_score": _score01(ai.get("hook_score")),
        "surprise_score": _score01(ai.get("surprise_score")),
        "humor_score": _score01(ai.get("humor_score")),
        "comment_potential": _score01(ai.get("comment_potential")),
        "rewatch_potential": _score01(ai.get("rewatch_potential")),
        "duration_signal": clamp(duration / 60.0),
    }


def _compilation_features(session: Session, compilation_id: uuid.UUID) -> dict[str, float]:
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
        "hook_score": clamp(average([_score01(item.get("hook_score")) for item in evidence])),
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
            average([float(segment.source_duration_seconds) for segment in segments]) / 60.0
        ),
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
    raise RuntimeError("publication source lineage is invalid")


def derive_performance_observations(channel_profile_id: uuid.UUID) -> int:
    created = 0
    with session_scope() as session:
        profile = session.get(ChannelProfile, channel_profile_id)
        if profile is None:
            raise ValueError(f"channel profile not found: {channel_profile_id}")
        publications = list(
            session.scalars(
                select(Publication).where(
                    Publication.youtube_connection_id == profile.youtube_connection_id
                )
            )
        )
        if not publications:
            return 0
        publication_by_id = {item.id: item for item in publications}
        snapshots = list(
            session.scalars(
                select(PublicationAnalyticsSnapshot)
                .where(PublicationAnalyticsSnapshot.publication_id.in_(publication_by_id))
                .order_by(PublicationAnalyticsSnapshot.sampled_at.asc())
            )
        )
        existing_snapshot_ids = (
            set(
                session.scalars(
                    select(PerformanceObservation.analytics_snapshot_id).where(
                        PerformanceObservation.analytics_snapshot_id.in_(
                            [item.id for item in snapshots]
                        )
                    )
                )
            )
            if snapshots
            else set()
        )

        for snapshot in snapshots:
            if snapshot.id in existing_snapshot_ids:
                continue
            publication = publication_by_id[snapshot.publication_id]
            anchor = publication.published_at or publication.publish_at or publication.created_at
            age_hours = max(
                1.0,
                (snapshot.sampled_at - anchor).total_seconds() / 3600.0,
            )
            source_kind, source_id, features = _publication_source_features(session, publication)
            views = int(snapshot.views or 0)
            engaged_views = int(snapshot.engaged_views or 0)
            score, signals = outcome_score(
                views=views,
                engaged_views=engaged_views,
                age_hours=age_hours,
                average_view_percentage=float(snapshot.average_view_percentage or 0),
                shares=int(snapshot.shares or 0),
                comments=int(snapshot.comments or 0),
                subscribers_gained=int(snapshot.subscribers_gained or 0),
            )
            labels: dict[str, object] = {
                **signals,
                "views": views,
                "engaged_views": engaged_views,
                "average_view_percentage": float(snapshot.average_view_percentage or 0),
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


def _latest_observations(
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


def train_channel_ranking(channel_profile_id: uuid.UUID) -> RankingSnapshot:
    with session_scope() as session:
        profile = session.get(ChannelProfile, channel_profile_id)
        if profile is None:
            raise ValueError(f"channel profile not found: {channel_profile_id}")
        observations = _latest_observations(session, profile.id)
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
        if session.get(ChannelProfile, channel_profile_id) is None:
            raise ValueError(f"channel profile not found: {channel_profile_id}")
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
                key: float(value) for key, value in snapshot.feature_means.items()
            },
            feature_scales={
                key: float(value) for key, value in snapshot.feature_scales.items()
            },
            coefficients={
                key: float(value) for key, value in snapshot.coefficients.items()
            },
            intercept=float(snapshot.intercept),
            confidence=float(snapshot.confidence),
            blend_ratio=float(snapshot.blend_ratio),
            validation_metrics=dict(snapshot.validation_metrics or {}),
        )
        return blended_score(features, result)


def _usage_events_for_sources(
    session: Session,
    production_ids: list[uuid.UUID],
    compilation_ids: list[uuid.UUID],
) -> list[UsageEvent]:
    conditions = []
    if production_ids:
        conditions.append(
            and_(
                UsageEvent.reference_type == "production",
                UsageEvent.reference_id.in_([str(item) for item in production_ids]),
            )
        )
    if compilation_ids:
        conditions.append(
            and_(
                UsageEvent.reference_type == "compilation",
                UsageEvent.reference_id.in_([str(item) for item in compilation_ids]),
            )
        )
    if not conditions:
        return []
    return list(session.scalars(select(UsageEvent).where(or_(*conditions))))


def compute_channel_economics(
    channel_profile_id: uuid.UUID,
    *,
    sample_key: str | None = None,
) -> ChannelEconomicsSnapshot:
    now = datetime.now(UTC)
    sample_key = sample_key or f"manual-{uuid.uuid4().hex}"
    month_start = datetime(now.year, now.month, 1, tzinfo=UTC)
    with session_scope() as session:
        profile = session.get(ChannelProfile, channel_profile_id)
        if profile is None:
            raise ValueError(f"channel profile not found: {channel_profile_id}")
        existing = session.scalar(
            select(ChannelEconomicsSnapshot).where(
                ChannelEconomicsSnapshot.channel_profile_id == profile.id,
                ChannelEconomicsSnapshot.sample_key == sample_key,
            )
        )
        if existing is not None:
            session.expunge(existing)
            return existing
        strategy = active_strategy(session, profile)
        connection = session.get(YouTubeConnection, profile.youtube_connection_id)
        if connection is None:
            raise RuntimeError("channel YouTube connection disappeared")
        publications = list(
            session.scalars(
                select(Publication).where(
                    Publication.youtube_connection_id == profile.youtube_connection_id
                )
            )
        )
        publication_ids = [item.id for item in publications]
        snapshots = (
            list(
                session.scalars(
                    select(PublicationAnalyticsSnapshot)
                    .where(PublicationAnalyticsSnapshot.publication_id.in_(publication_ids))
                    .order_by(PublicationAnalyticsSnapshot.sampled_at.desc())
                )
            )
            if publication_ids
            else []
        )
        latest: dict[uuid.UUID, PublicationAnalyticsSnapshot] = {}
        for snapshot in snapshots:
            latest.setdefault(snapshot.publication_id, snapshot)
        revenue = sum(
            (snapshot.estimated_revenue or Decimal("0") for snapshot in latest.values()),
            Decimal("0"),
        )
        production_ids = [item.production_id for item in publications if item.production_id]
        compilation_ids = [item.compilation_id for item in publications if item.compilation_id]
        usage = _usage_events_for_sources(session, production_ids, compilation_ids)

        allocation: dict[tuple[str, uuid.UUID], int] = {}
        if production_ids:
            for source_id, count in session.execute(
                select(
                    Publication.production_id,
                    func.count(func.distinct(Publication.youtube_connection_id)),
                )
                .where(Publication.production_id.in_(production_ids))
                .group_by(Publication.production_id)
            ):
                if source_id is not None:
                    allocation[("production", source_id)] = max(int(count), 1)
        if compilation_ids:
            for source_id, count in session.execute(
                select(
                    Publication.compilation_id,
                    func.count(func.distinct(Publication.youtube_connection_id)),
                )
                .where(Publication.compilation_id.in_(compilation_ids))
                .group_by(Publication.compilation_id)
            ):
                if source_id is not None:
                    allocation[("compilation", source_id)] = max(int(count), 1)

        total_cost = Decimal("0")
        month_cost = Decimal("0")
        for event in usage:
            try:
                source_id = uuid.UUID(str(event.reference_id))
            except (TypeError, ValueError):
                continue
            divisor = Decimal(allocation.get((str(event.reference_type), source_id), 1))
            attributed = Decimal(event.cost_usd) / divisor
            total_cost += attributed
            if event.created_at >= month_start:
                month_cost += attributed

        margin = revenue - total_cost
        reinvestable = min(
            strategy.reinvestment_cap_usd,
            max(Decimal("0"), margin) * strategy.reinvestment_rate,
        )
        effective_allowance = strategy.monthly_hard_budget_usd + reinvestable
        headroom = max(Decimal("0"), effective_allowance - month_cost)
        monetary_available = MONETARY_SCOPE in set(connection.scopes or [])
        economics = ChannelEconomicsSnapshot(
            channel_profile_id=profile.id,
            sample_key=sample_key,
            sampled_at=now,
            revenue_usd=revenue,
            attributed_ai_cost_usd=total_cost,
            contribution_margin_usd=margin,
            reinvestable_usd=reinvestable,
            hard_budget_usd=strategy.monthly_hard_budget_usd,
            month_to_date_spend_usd=month_cost,
            budget_headroom_usd=headroom,
            monetary_scope_available=monetary_available,
            details={
                "revenue_basis": "latest_cumulative_estimated_revenue_per_publication",
                "cost_attribution": (
                    "source_cost_divided_across_distinct_publishing_channels"
                ),
                "effective_allowance_usd": str(effective_allowance),
                "publication_count": len(publications),
                "monetary_snapshot_count": sum(
                    item.estimated_revenue is not None for item in latest.values()
                ),
            },
        )
        session.add(economics)
        session.add(
            DomainEvent(
                aggregate_type="channel_profile",
                aggregate_id=str(profile.id),
                event_type="channel_profile.economics_sampled",
                payload={
                    "channel_profile_id": str(profile.id),
                    "sample_key": sample_key,
                    "revenue_usd": str(revenue),
                    "attributed_ai_cost_usd": str(total_cost),
                    "budget_headroom_usd": str(headroom),
                },
            )
        )
        session.flush()
        session.refresh(economics)
        session.expunge(economics)
        return economics


def _is_blackout(strategy: ChannelStrategyVersion, weekday: int, hour_local: int) -> bool:
    return any(
        int(item.get("weekday", -1)) == weekday
        and int(item.get("hour_local", -1)) == hour_local
        for item in (strategy.blackout_windows or [])
    )


def generate_schedule_recommendations(
    channel_profile_id: uuid.UUID,
    *,
    run_key: str | None = None,
    limit: int = 5,
) -> list[ScheduleRecommendation]:
    run_key = run_key or f"manual-{uuid.uuid4().hex}"
    with session_scope() as session:
        profile = session.get(ChannelProfile, channel_profile_id)
        if profile is None:
            raise ValueError(f"channel profile not found: {channel_profile_id}")
        strategy = active_strategy(session, profile)
        existing = list(
            session.scalars(
                select(ScheduleRecommendation)
                .where(
                    ScheduleRecommendation.channel_profile_id == profile.id,
                    ScheduleRecommendation.run_key == run_key,
                )
                .order_by(ScheduleRecommendation.rank)
            )
        )
        if existing:
            for item in existing:
                session.expunge(item)
            return existing
        observations = _latest_observations(session, profile.id)
        publication_ids = [item.publication_id for item in observations]
        publications = (
            {
                item.id: item
                for item in session.scalars(
                    select(Publication).where(Publication.id.in_(publication_ids))
                )
            }
            if publication_ids
            else {}
        )
        timezone = ZoneInfo(profile.timezone)
        history: list[HistoricalSlot] = []
        for observation in observations:
            publication = publications.get(observation.publication_id)
            if publication is None:
                continue
            anchor = publication.published_at or publication.publish_at or publication.created_at
            local = anchor.astimezone(timezone)
            history.append(
                HistoricalSlot(
                    weekday=local.weekday(),
                    hour_local=local.hour,
                    outcome_score=float(observation.outcome_score),
                )
            )
        windows = recommend_windows(
            history,
            fallback_schedule=list(strategy.fallback_schedule or []),
            limit=max(limit * 2, limit),
        )
        windows = [
            item
            for item in windows
            if not _is_blackout(strategy, item.weekday, item.hour_local)
        ][:limit]
        rows: list[ScheduleRecommendation] = []
        for rank, item in enumerate(windows, start=1):
            row = ScheduleRecommendation(
                channel_profile_id=profile.id,
                run_key=run_key,
                rank=rank,
                weekday=item.weekday,
                hour_local=item.hour_local,
                score=_decimal(item.score),
                sample_count=item.sample_count,
                confidence=_decimal(item.confidence),
                source=item.source,
                recommendation_metadata={
                    "timezone": profile.timezone,
                    "history_samples": len(history),
                    "strategy_version": strategy.version,
                },
            )
            session.add(row)
            rows.append(row)
        session.add(
            DomainEvent(
                aggregate_type="channel_profile",
                aggregate_id=str(profile.id),
                event_type="channel_profile.schedule_recommended",
                payload={
                    "channel_profile_id": str(profile.id),
                    "run_key": run_key,
                    "recommendation_count": len(rows),
                    "history_samples": len(history),
                },
            )
        )
        session.flush()
        for row in rows:
            session.refresh(row)
            session.expunge(row)
        return rows


def refresh_channel_intelligence(
    channel_profile_id: uuid.UUID,
    *,
    refresh_key: str | None = None,
) -> dict[str, object]:
    key = refresh_key or uuid.uuid4().hex
    observations = derive_performance_observations(channel_profile_id)
    ranking = train_channel_ranking(channel_profile_id)
    economics = compute_channel_economics(
        channel_profile_id,
        sample_key=f"refresh-{key}",
    )
    schedule = generate_schedule_recommendations(
        channel_profile_id,
        run_key=f"refresh-{key}",
    )
    return {
        "channel_profile_id": str(channel_profile_id),
        "created_observations": observations,
        "ranking_snapshot_id": str(ranking.id),
        "ranking_version": ranking.version,
        "ranking_confidence": float(ranking.confidence),
        "economics_snapshot_id": str(economics.id),
        "schedule_recommendations": len(schedule),
    }
