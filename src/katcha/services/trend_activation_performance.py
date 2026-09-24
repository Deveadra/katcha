from __future__ import annotations

import uuid
from collections import Counter
from datetime import UTC, datetime
from decimal import Decimal
from statistics import median
from typing import Any

from sqlalchemy import func, select

from katcha.db import session_scope
from katcha.intelligence_models import ChannelEconomicsSnapshot, ChannelProfile
from katcha.models import DomainEvent
from katcha.publishing_models import Publication, PublicationAnalyticsSnapshot
from katcha.services.channel_economics import latest_economics_snapshot
from katcha.services.trend_calibration import (
    derive_trend_outcomes,
    latest_trend_calibration,
)
from katcha.short_episode_models import ShortEpisode
from katcha.trend_activation_models import (
    TrendActivationDecision,
    TrendActivationPerformanceSnapshot,
    TrendActivationPolicyVersion,
)
from katcha.trend_calibration_models import TrendOpportunityOutcome
from katcha.trend_models import TrendOpportunity

_TARGET_OUTCOME_AGE_HOURS = 24
_MIN_PUBLISHED_FOR_RECOMMENDATION = 8
_MIN_OUTCOMES_FOR_RECOMMENDATION = 5
_MIN_REVENUE_COVERAGE = 0.50


def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _decimal(value: float | Decimal | int) -> Decimal:
    return Decimal(str(round(float(value), 8)))


def _minutes(later: datetime | None, earlier: datetime | None) -> float | None:
    later_utc = _utc(later)
    earlier_utc = _utc(earlier)
    if later_utc is None or earlier_utc is None or later_utc < earlier_utc:
        return None
    return (later_utc - earlier_utc).total_seconds() / 60.0


def _median_decimal(values: list[float]) -> Decimal | None:
    if not values:
        return None
    return _decimal(median(values))


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def recommend_activation_policy(
    metrics: dict[str, Any],
    policy: TrendActivationPolicyVersion | None,
) -> tuple[str, dict[str, Any]]:
    evidence = {
        "published_count": int(metrics.get("published_count") or 0),
        "outcome_count": int(metrics.get("outcome_count") or 0),
        "revenue_coverage": float(metrics.get("revenue_coverage") or 0.0),
        "plan_to_publish_rate": float(metrics.get("plan_to_publish_rate") or 0.0),
        "mean_lift_ratio": metrics.get("mean_lift_ratio"),
        "contribution_margin_usd": str(
            metrics.get("contribution_margin_usd") or Decimal("0")
        ),
        "capacity_miss_count": int(metrics.get("capacity_miss_count") or 0),
    }
    if policy is None:
        return "no_policy", {
            "reason": "activation_policy_not_configured",
            "proposed_changes": {},
            "evidence": evidence,
        }

    published_count = evidence["published_count"]
    outcome_count = evidence["outcome_count"]
    if (
        published_count < _MIN_PUBLISHED_FOR_RECOMMENDATION
        or outcome_count < _MIN_OUTCOMES_FOR_RECOMMENDATION
    ):
        return "insufficient_data", {
            "reason": "more_published_activation_outcomes_required",
            "proposed_changes": {},
            "evidence": {
                **evidence,
                "minimum_published": _MIN_PUBLISHED_FOR_RECOMMENDATION,
                "minimum_outcomes": _MIN_OUTCOMES_FOR_RECOMMENDATION,
            },
        }

    revenue_coverage = evidence["revenue_coverage"]
    plan_to_publish = evidence["plan_to_publish_rate"]
    mean_lift_raw = evidence["mean_lift_ratio"]
    mean_lift = float(mean_lift_raw) if mean_lift_raw is not None else None
    margin = Decimal(str(metrics.get("contribution_margin_usd") or 0))
    changes: dict[str, Any] = {}

    if plan_to_publish < 0.60:
        if policy.max_activations_per_day > 1:
            changes["max_activations_per_day"] = policy.max_activations_per_day - 1
        if policy.max_backlog > 1:
            changes["max_backlog"] = policy.max_backlog - 1
        return "production_bottleneck", {
            "reason": "plans_are_not_reaching_publication_reliably",
            "proposed_changes": changes,
            "evidence": evidence,
        }

    if revenue_coverage < _MIN_REVENUE_COVERAGE:
        return "insufficient_economics", {
            "reason": "monetary_analytics_coverage_too_low_for_profit_optimization",
            "proposed_changes": {},
            "evidence": {
                **evidence,
                "minimum_revenue_coverage": _MIN_REVENUE_COVERAGE,
            },
        }

    if margin <= 0 or (mean_lift is not None and mean_lift < 1.0):
        changes["min_opportunity_score"] = round(
            _clamp(float(policy.min_opportunity_score) + 0.03, 0.0, 0.95),
            4,
        )
        changes["min_confidence"] = round(
            _clamp(float(policy.min_confidence) + 0.02, 0.0, 0.95),
            4,
        )
        if policy.max_activations_per_day > 1:
            changes["max_activations_per_day"] = policy.max_activations_per_day - 1
        return "tighten_quality", {
            "reason": "activated_opportunities_are_not_yet_profitable_or_lifting_baseline",
            "proposed_changes": changes,
            "evidence": evidence,
        }

    capacity_misses = evidence["capacity_miss_count"]
    if (
        margin > 0
        and mean_lift is not None
        and mean_lift >= 1.10
        and plan_to_publish >= 0.75
        and capacity_misses > 0
    ):
        changes["max_activations_per_day"] = min(
            policy.max_activations_per_day + 1,
            100,
        )
        changes["max_backlog"] = min(policy.max_backlog + 1, 1000)
        changes["cooldown_minutes"] = max(0, policy.cooldown_minutes - 30)
        return "expand_capacity", {
            "reason": "profitable_high_lift_opportunities_are_hitting_capacity_limits",
            "proposed_changes": changes,
            "evidence": evidence,
        }

    return "hold", {
        "reason": "current_policy_is_within_observed_quality_and_capacity_bounds",
        "proposed_changes": {},
        "evidence": evidence,
    }


def latest_activation_performance(
    channel_profile_id: uuid.UUID,
) -> TrendActivationPerformanceSnapshot | None:
    with session_scope() as session:
        return session.scalar(
            select(TrendActivationPerformanceSnapshot)
            .where(
                TrendActivationPerformanceSnapshot.channel_profile_id
                == channel_profile_id
            )
            .order_by(TrendActivationPerformanceSnapshot.version.desc())
            .limit(1)
        )


def list_activation_performance(
    channel_profile_id: uuid.UUID,
    *,
    limit: int = 50,
) -> list[TrendActivationPerformanceSnapshot]:
    with session_scope() as session:
        return list(
            session.scalars(
                select(TrendActivationPerformanceSnapshot)
                .where(
                    TrendActivationPerformanceSnapshot.channel_profile_id
                    == channel_profile_id
                )
                .order_by(TrendActivationPerformanceSnapshot.version.desc())
                .limit(max(1, min(limit, 250)))
            )
        )


def refresh_activation_performance(
    channel_profile_id: uuid.UUID,
    *,
    run_key: str,
) -> TrendActivationPerformanceSnapshot:
    key = run_key.strip()
    if not key:
        raise ValueError("activation performance run_key is required")
    if len(key) > 160:
        raise ValueError("activation performance run_key must be 160 characters or fewer")

    derive_trend_outcomes(channel_profile_id)

    with session_scope() as session:
        profile = session.get(ChannelProfile, channel_profile_id)
        if profile is None:
            raise ValueError(f"channel profile not found: {channel_profile_id}")
        existing = session.scalar(
            select(TrendActivationPerformanceSnapshot).where(
                TrendActivationPerformanceSnapshot.channel_profile_id
                == channel_profile_id,
                TrendActivationPerformanceSnapshot.run_key == key,
            )
        )
        if existing is not None:
            return existing

        decisions = list(
            session.scalars(
                select(TrendActivationDecision)
                .where(TrendActivationDecision.channel_profile_id == channel_profile_id)
                .order_by(
                    TrendActivationDecision.trend_opportunity_id,
                    TrendActivationDecision.created_at,
                )
            )
        )
        opportunity_ids = sorted(
            {row.trend_opportunity_id for row in decisions},
            key=str,
        )
        opportunities = {
            row.id: row
            for row in session.scalars(
                select(TrendOpportunity).where(
                    TrendOpportunity.id.in_(opportunity_ids)
                )
            )
        } if opportunity_ids else {}

        episodes = list(
            session.scalars(
                select(ShortEpisode)
                .where(
                    ShortEpisode.channel_profile_id == channel_profile_id,
                    ShortEpisode.trend_opportunity_id.in_(opportunity_ids)
                    if opportunity_ids
                    else ShortEpisode.id.is_(None),
                )
                .order_by(ShortEpisode.created_at)
            )
        )
        episodes_by_opportunity: dict[uuid.UUID, list[ShortEpisode]] = {}
        episode_by_id: dict[uuid.UUID, ShortEpisode] = {}
        for episode in episodes:
            episode_by_id[episode.id] = episode
            if episode.trend_opportunity_id is not None:
                episodes_by_opportunity.setdefault(
                    episode.trend_opportunity_id,
                    [],
                ).append(episode)

        episode_ids = list(episode_by_id)
        publications = list(
            session.scalars(
                select(Publication)
                .where(
                    Publication.short_episode_id.in_(episode_ids)
                    if episode_ids
                    else Publication.id.is_(None)
                )
                .order_by(Publication.created_at)
            )
        )
        publications_by_episode: dict[uuid.UUID, list[Publication]] = {}
        for publication in publications:
            if publication.short_episode_id is not None:
                publications_by_episode.setdefault(
                    publication.short_episode_id,
                    [],
                ).append(publication)

        publication_ids = [item.id for item in publications]
        analytics = list(
            session.scalars(
                select(PublicationAnalyticsSnapshot)
                .where(
                    PublicationAnalyticsSnapshot.publication_id.in_(publication_ids)
                    if publication_ids
                    else PublicationAnalyticsSnapshot.id.is_(None)
                )
                .order_by(
                    PublicationAnalyticsSnapshot.publication_id,
                    PublicationAnalyticsSnapshot.sampled_at,
                )
            )
        )
        latest_analytics: dict[uuid.UUID, PublicationAnalyticsSnapshot] = {}
        for item in analytics:
            current = latest_analytics.get(item.publication_id)
            if current is None or item.sampled_at > current.sampled_at:
                latest_analytics[item.publication_id] = item

        outcomes = list(
            session.scalars(
                select(TrendOpportunityOutcome).where(
                    TrendOpportunityOutcome.channel_profile_id == channel_profile_id,
                    TrendOpportunityOutcome.age_bucket_hours
                    == _TARGET_OUTCOME_AGE_HOURS,
                    TrendOpportunityOutcome.trend_opportunity_id.in_(opportunity_ids)
                    if opportunity_ids
                    else TrendOpportunityOutcome.id.is_(None),
                )
            )
        )
        outcome_by_opportunity = {
            item.trend_opportunity_id: item for item in outcomes
        }

        policy = session.scalar(
            select(TrendActivationPolicyVersion)
            .where(TrendActivationPolicyVersion.channel_profile_id == channel_profile_id)
            .order_by(TrendActivationPolicyVersion.version.desc())
            .limit(1)
        )
        calibration = latest_trend_calibration(channel_profile_id)
        economics = latest_economics_snapshot(session, profile)

        decisions_by_opportunity: dict[
            uuid.UUID, list[TrendActivationDecision]
        ] = {}
        for decision in decisions:
            decisions_by_opportunity.setdefault(
                decision.trend_opportunity_id,
                [],
            ).append(decision)

        planned_opportunities: set[uuid.UUID] = set()
        published_opportunities: set[uuid.UUID] = set()
        missed_reason_counts: Counter[str] = Counter()
        opportunity_to_plan: list[float] = []
        plan_to_publish: list[float] = []
        opportunity_to_publish: list[float] = []
        expiry_headroom_at_plan: list[float] = []
        expiry_headroom_at_publish: list[float] = []
        planned_episode_ids: set[uuid.UUID] = set()
        published_publication_ids: set[uuid.UUID] = set()
        revenue = Decimal("0")
        latest_views = 0
        revenue_covered_publications = 0

        for opportunity_id, rows in decisions_by_opportunity.items():
            opportunity = opportunities.get(opportunity_id)
            if opportunity is None:
                continue
            root_episodes = [
                item
                for item in episodes_by_opportunity.get(opportunity_id, [])
                if item.parent_episode_id is None and item.generation == 1
            ]
            root_episode = root_episodes[0] if root_episodes else None
            if root_episode is None:
                missed_reason_counts[rows[-1].decision] += 1
                continue

            planned_opportunities.add(opportunity_id)
            planned_episode_ids.add(root_episode.id)
            plan_latency = _minutes(root_episode.created_at, opportunity.created_at)
            if plan_latency is not None:
                opportunity_to_plan.append(plan_latency)
            expires_at = _utc(opportunity.expires_at)
            plan_at = _utc(root_episode.created_at)
            if expires_at is not None and plan_at is not None:
                expiry_headroom_at_plan.append(
                    max(0.0, (expires_at - plan_at).total_seconds() / 60.0)
                )

            candidate_publications: list[Publication] = []
            for episode in episodes_by_opportunity.get(opportunity_id, []):
                candidate_publications.extend(
                    publications_by_episode.get(episode.id, [])
                )
            actual_publications = [
                item
                for item in candidate_publications
                if item.published_at is not None
            ]
            if not actual_publications:
                continue
            publication = min(
                actual_publications,
                key=lambda item: _utc(item.published_at) or datetime.max.replace(tzinfo=UTC),
            )
            published_at = _utc(publication.published_at)
            if published_at is None:
                continue
            published_opportunities.add(opportunity_id)
            published_publication_ids.add(publication.id)
            plan_publish_latency = _minutes(published_at, root_episode.created_at)
            opportunity_publish_latency = _minutes(
                published_at,
                opportunity.created_at,
            )
            if plan_publish_latency is not None:
                plan_to_publish.append(plan_publish_latency)
            if opportunity_publish_latency is not None:
                opportunity_to_publish.append(opportunity_publish_latency)
            if expires_at is not None:
                expiry_headroom_at_publish.append(
                    (expires_at - published_at).total_seconds() / 60.0
                )

            snapshot = latest_analytics.get(publication.id)
            if snapshot is not None:
                latest_views += int(snapshot.views or 0)
                if snapshot.estimated_revenue is not None:
                    revenue += Decimal(snapshot.estimated_revenue)
                    revenue_covered_publications += 1

        cost = sum(
            (
                Decimal(episode_by_id[episode_id].estimated_cost_usd or 0)
                for episode_id in planned_episode_ids
                if episode_id in episode_by_id
            ),
            Decimal("0"),
        )
        margin = revenue - cost

        lift_values = [
            float(item.lift_ratio)
            for opportunity_id, item in outcome_by_opportunity.items()
            if opportunity_id in published_opportunities
            and item.lift_ratio is not None
        ]
        labeled_outcomes = [
            item
            for opportunity_id, item in outcome_by_opportunity.items()
            if opportunity_id in published_opportunities
            and item.realized_breakout is not None
        ]
        breakout_count = sum(1 for item in labeled_outcomes if item.realized_breakout)
        outcome_count = sum(
            1
            for opportunity_id in outcome_by_opportunity
            if opportunity_id in published_opportunities
        )

        decision_count = len(decisions_by_opportunity)
        planned_count = len(planned_opportunities)
        published_count = len(published_opportunities)
        opportunity_to_plan_rate = (
            planned_count / decision_count if decision_count else 0.0
        )
        plan_to_publish_rate = (
            published_count / planned_count if planned_count else 0.0
        )
        revenue_coverage = (
            revenue_covered_publications / published_count
            if published_count
            else 0.0
        )
        capacity_miss_count = sum(
            missed_reason_counts[key]
            for key in ("daily_cap", "backlog", "cooldown")
        )
        mean_lift = (
            sum(lift_values) / len(lift_values) if lift_values else None
        )
        breakout_rate = (
            breakout_count / len(labeled_outcomes) if labeled_outcomes else None
        )

        funnel = {
            "decision_count": decision_count,
            "planned_count": planned_count,
            "published_count": published_count,
            "outcome_count": outcome_count,
            "opportunity_to_plan_rate": round(opportunity_to_plan_rate, 6),
            "plan_to_publish_rate": round(plan_to_publish_rate, 6),
            "revenue_coverage": round(revenue_coverage, 6),
            "revenue_covered_publications": revenue_covered_publications,
            "monetary_scope_available": (
                bool(economics.monetary_scope_available) if economics else False
            ),
            "capacity_miss_count": capacity_miss_count,
            "mean_expiry_headroom_at_plan_minutes": (
                round(sum(expiry_headroom_at_plan) / len(expiry_headroom_at_plan), 4)
                if expiry_headroom_at_plan
                else None
            ),
            "mean_expiry_headroom_at_publish_minutes": (
                round(
                    sum(expiry_headroom_at_publish)
                    / len(expiry_headroom_at_publish),
                    4,
                )
                if expiry_headroom_at_publish
                else None
            ),
            "budget_headroom_usd": (
                str(economics.budget_headroom_usd) if economics else None
            ),
            "economics_contribution_margin_usd": (
                str(economics.contribution_margin_usd) if economics else None
            ),
            "target_outcome_age_hours": _TARGET_OUTCOME_AGE_HOURS,
        }
        recommendation_metrics = {
            **funnel,
            "contribution_margin_usd": margin,
            "mean_lift_ratio": mean_lift,
        }
        recommendation_status, recommendation = recommend_activation_policy(
            recommendation_metrics,
            policy,
        )

        version = int(
            session.scalar(
                select(
                    func.coalesce(
                        func.max(TrendActivationPerformanceSnapshot.version),
                        0,
                    )
                ).where(
                    TrendActivationPerformanceSnapshot.channel_profile_id
                    == channel_profile_id
                )
            )
            or 0
        ) + 1
        sample_times = [_utc(item.created_at) for item in decisions]
        snapshot = TrendActivationPerformanceSnapshot(
            channel_profile_id=channel_profile_id,
            version=version,
            run_key=key,
            policy_version=policy.version if policy else None,
            calibration_version=calibration.version if calibration else None,
            economics_snapshot_id=economics.id if economics else None,
            decision_count=decision_count,
            planned_count=planned_count,
            published_count=published_count,
            outcome_count=outcome_count,
            opportunity_to_plan_rate=_decimal(opportunity_to_plan_rate),
            plan_to_publish_rate=_decimal(plan_to_publish_rate),
            median_opportunity_to_plan_minutes=_median_decimal(
                opportunity_to_plan
            ),
            median_plan_to_publish_minutes=_median_decimal(plan_to_publish),
            median_opportunity_to_publish_minutes=_median_decimal(
                opportunity_to_publish
            ),
            revenue_usd=revenue,
            attributed_cost_usd=cost,
            contribution_margin_usd=margin,
            latest_views=latest_views,
            mean_lift_ratio=(
                _decimal(mean_lift) if mean_lift is not None else None
            ),
            realized_breakout_rate=(
                _decimal(breakout_rate) if breakout_rate is not None else None
            ),
            missed_reason_counts=dict(sorted(missed_reason_counts.items())),
            funnel_metrics=funnel,
            recommendation_status=recommendation_status,
            recommendation=recommendation,
            sample_window_start=min(
                (value for value in sample_times if value is not None),
                default=None,
            ),
            sample_window_end=max(
                (value for value in sample_times if value is not None),
                default=None,
            ),
        )
        session.add(snapshot)
        session.flush()
        session.add(
            DomainEvent(
                aggregate_type="channel_profile",
                aggregate_id=str(channel_profile_id),
                event_type="trend.activation.performance_refreshed",
                payload={
                    "channel_profile_id": str(channel_profile_id),
                    "performance_snapshot_id": str(snapshot.id),
                    "version": snapshot.version,
                    "run_key": key,
                    "decision_count": decision_count,
                    "planned_count": planned_count,
                    "published_count": published_count,
                    "contribution_margin_usd": str(margin),
                    "recommendation_status": recommendation_status,
                },
            )
        )
        return snapshot
