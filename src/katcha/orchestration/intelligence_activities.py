from __future__ import annotations

import uuid

from temporalio import activity

from katcha.services.channel_automation import maybe_auto_demote
from katcha.services.channel_economics import compute_channel_economics
from katcha.services.channel_learning import (
    derive_performance_observations,
    train_channel_ranking,
)
from katcha.services.channel_scheduling import compute_schedule_recommendations
from katcha.services.trend_activation_performance import refresh_activation_performance
from katcha.services.trend_auto_activation import run_autonomous_trend_activation


@activity.defn
def derive_channel_observations_activity(channel_profile_id: str) -> dict[str, object]:
    profile_id = uuid.UUID(channel_profile_id)
    created = derive_performance_observations(profile_id)
    return {
        "channel_profile_id": channel_profile_id,
        "created_observations": created,
    }


@activity.defn
def train_channel_ranking_activity(
    channel_profile_id: str,
    run_key: str,
) -> dict[str, object]:
    profile_id = uuid.UUID(channel_profile_id)
    snapshot = train_channel_ranking(profile_id, run_key=run_key)
    return {
        "channel_profile_id": channel_profile_id,
        "ranking_snapshot_id": str(snapshot.id),
        "ranking_version": snapshot.version,
        "sample_count": snapshot.sample_count,
        "confidence": float(snapshot.confidence),
        "blend_ratio": float(snapshot.blend_ratio),
        "algorithm": snapshot.algorithm,
    }


@activity.defn
def compute_channel_economics_activity(
    channel_profile_id: str,
    run_key: str,
) -> dict[str, object]:
    profile_id = uuid.UUID(channel_profile_id)
    snapshot = compute_channel_economics(profile_id, sample_key=run_key)
    return {
        "channel_profile_id": channel_profile_id,
        "economics_snapshot_id": str(snapshot.id),
        "revenue_usd": str(snapshot.revenue_usd),
        "spend_usd": str(snapshot.month_to_date_spend_usd),
        "margin_usd": str(snapshot.contribution_margin_usd),
        "reinvestable_usd": str(snapshot.reinvestable_usd),
        "budget_headroom_usd": str(snapshot.budget_headroom_usd),
    }


@activity.defn
def compute_channel_schedule_activity(
    channel_profile_id: str,
    run_key: str,
) -> dict[str, object]:
    profile_id = uuid.UUID(channel_profile_id)
    rows = compute_schedule_recommendations(profile_id, run_key=run_key)
    return {
        "channel_profile_id": channel_profile_id,
        "recommendation_count": len(rows),
        "recommendations": [
            {
                "rank": row.rank,
                "weekday": row.weekday,
                "hour_local": row.hour_local,
                "score": float(row.score),
                "confidence": float(row.confidence),
                "source": row.source,
            }
            for row in rows
        ],
    }


@activity.defn
def apply_channel_safety_demotion_activity(
    channel_profile_id: str,
) -> dict[str, object]:
    profile_id = uuid.UUID(channel_profile_id)
    row = maybe_auto_demote(profile_id)
    return {
        "channel_profile_id": channel_profile_id,
        "demoted": row is not None,
        "automation_version": row.version if row else None,
        "automation_level": row.level if row else None,
    }


@activity.defn
def run_channel_trend_activation_activity(
    channel_profile_id: str,
    run_key: str,
) -> dict[str, object]:
    summary = run_autonomous_trend_activation(
        uuid.UUID(channel_profile_id),
        run_key=run_key,
    )
    return summary.as_dict()


@activity.defn
def refresh_trend_activation_performance_activity(
    channel_profile_id: str,
    run_key: str,
) -> dict[str, object]:
    snapshot = refresh_activation_performance(
        uuid.UUID(channel_profile_id),
        run_key=run_key,
    )
    return {
        "channel_profile_id": channel_profile_id,
        "performance_snapshot_id": str(snapshot.id),
        "version": snapshot.version,
        "decision_count": snapshot.decision_count,
        "planned_count": snapshot.planned_count,
        "published_count": snapshot.published_count,
        "contribution_margin_usd": str(snapshot.contribution_margin_usd),
        "recommendation_status": snapshot.recommendation_status,
    }
