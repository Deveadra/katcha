from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import select
from temporalio import activity

from katcha.db import session_scope
from katcha.orchestration.client import (
    start_packaging_activation_workflow,
    start_reach_sync_workflow,
)
from katcha.packaging_intelligence_models import PackagingIntelligenceSnapshot
from katcha.packaging_models import PackagingExperiment, PublicationPackagingActivation
from katcha.services.channel_automation import maybe_auto_demote
from katcha.services.channel_economics import compute_channel_economics
from katcha.services.channel_learning import (
    derive_performance_observations,
    train_channel_ranking,
)
from katcha.services.channel_scheduling import compute_schedule_recommendations
from katcha.services.edit_blueprint_performance import (
    refresh_edit_blueprint_performance,
)
from katcha.services.packaging_experiments import (
    reconcile_packaging_experiment,
    start_packaging_experiment,
)
from katcha.services.packaging_intelligence import refresh_packaging_intelligence
from katcha.services.reach_cadence import eligible_reach_connection, reach_sync_identity
from katcha.services.trend_activation_performance import refresh_activation_performance
from katcha.services.trend_auto_activation import run_autonomous_trend_activation


@activity.defn
async def schedule_channel_reach_sync_activity(
    channel_profile_id: str, at: str
) -> dict[str, object]:
    connection_id, reason = eligible_reach_connection(uuid.UUID(channel_profile_id))
    if connection_id is None:
        return {"status": "skipped", "reason": reason}
    workflow_id = reach_sync_identity(connection_id, datetime.fromisoformat(at))
    await start_reach_sync_workflow(str(connection_id), workflow_id)
    return {
        "status": "scheduled",
        "connection_id": str(connection_id),
        "workflow_id": workflow_id,
    }


@activity.defn
async def run_channel_packaging_experiments_activity(
    channel_profile_id: str, at: str
) -> dict[str, object]:
    channel_id = uuid.UUID(channel_profile_id)
    reference = datetime.fromisoformat(at)
    with session_scope() as session:
        active_ids = list(
            session.scalars(
                select(PackagingExperiment.id)
                .where(
                    PackagingExperiment.channel_profile_id == channel_id,
                    PackagingExperiment.status.in_(
                        ("preparing", "pending", "observing", "rollback_pending")
                    ),
                )
                .limit(100)
            )
        )
        snapshot = session.scalar(
            select(PackagingIntelligenceSnapshot)
            .where(
                PackagingIntelligenceSnapshot.channel_profile_id == channel_id,
            )
            .order_by(
                PackagingIntelligenceSnapshot.created_at.desc(),
                PackagingIntelligenceSnapshot.version.desc(),
            )
            .limit(1)
        )
        proposals = list(snapshot.recommendations or []) if snapshot else []

    started: list[str] = []
    advanced: list[str] = []
    blocked: list[dict[str, str]] = []

    async def launch(row: PackagingExperiment) -> None:
        activation_id = (
            row.rollback_activation_id
            if row.status == "rollback_pending"
            else row.activation_id
            if row.status == "pending"
            else None
        )
        if activation_id is None:
            return
        with session_scope() as session:
            activation = session.get(PublicationPackagingActivation, activation_id)
            if activation is None or activation.status not in {"queued", "running"}:
                return
            workflow_id = activation.workflow_id
        await start_packaging_activation_workflow(str(activation_id), workflow_id)

    for experiment_id in active_ids:
        try:
            row = reconcile_packaging_experiment(experiment_id, now=reference)
            await launch(row)
            advanced.append(str(row.id))
        except (ValueError, RuntimeError) as exc:
            blocked.append({"experiment_id": str(experiment_id), "reason": str(exc)[:160]})

    for proposal in proposals[:100]:
        if proposal.get("recommendation_type") != "test":
            continue
        try:
            publication_id = uuid.UUID(str(proposal["publication_id"]))
            candidate_id = uuid.UUID(str(proposal["candidate_variant_id"]))
            row = start_packaging_experiment(
                publication_id, candidate_id, now=reference, expected_channel_id=channel_id
            )
            await launch(row)
            started.append(str(row.id))
        except (ValueError, RuntimeError, KeyError) as exc:
            blocked.append(
                {"publication_id": str(proposal.get("publication_id")), "reason": str(exc)[:160]}
            )
    return {"started": started, "advanced": advanced, "blocked": blocked[:100]}


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
def refresh_edit_blueprint_performance_activity(
    channel_profile_id: str,
    run_key: str,
) -> dict[str, object]:
    snapshot = refresh_edit_blueprint_performance(
        uuid.UUID(channel_profile_id),
        run_key=run_key,
    )
    return {
        "channel_profile_id": channel_profile_id,
        "snapshot_id": str(snapshot.id),
        "version": snapshot.version,
        "age_bucket_hours": snapshot.age_bucket_hours,
        "publication_count": snapshot.publication_count,
        "blueprint_group_count": snapshot.blueprint_group_count,
        "monetary_coverage": float(snapshot.monetary_coverage),
        "retention_coverage": float(snapshot.retention_coverage),
        "comparison_status": snapshot.comparison_status,
    }


@activity.defn
def refresh_packaging_intelligence_activity(
    channel_profile_id: str,
    run_key: str,
) -> dict[str, object]:
    snapshot = refresh_packaging_intelligence(
        uuid.UUID(channel_profile_id),
        run_key=run_key,
    )
    return {
        "channel_profile_id": channel_profile_id,
        "snapshot_id": str(snapshot.id),
        "version": snapshot.version,
        "maturity_days": snapshot.maturity_days,
        "publication_count": snapshot.publication_count,
        "variant_window_count": snapshot.variant_window_count,
        "recommendation_count": snapshot.recommendation_count,
        "recommendation_status": snapshot.recommendation_status,
    }


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
