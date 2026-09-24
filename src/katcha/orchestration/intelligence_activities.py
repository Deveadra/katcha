from __future__ import annotations

import uuid
from datetime import datetime

from temporalio import activity

from katcha.orchestration.client import (
    start_packaging_activation_workflow,
    start_reach_sync_workflow,
)
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
    active_channel_experiments,
    channel_experiment_candidates,
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
async def run_packaging_experiments_activity(
    channel_profile_id: str,
) -> dict[str, object]:
    profile_id = uuid.UUID(channel_profile_id)
    reconciled: list[dict[str, object]] = []
    for experiment in active_channel_experiments(profile_id):
        action = reconcile_packaging_experiment(experiment.id)
        if action.activation is not None and action.activation.status in {"queued", "running"}:
            await start_packaging_activation_workflow(
                str(action.activation.id),
                action.activation.workflow_id,
            )
        reconciled.append(
            {
                "experiment_id": str(experiment.id),
                "action": action.action,
                "reason": action.reason,
                "activation_id": (
                    str(action.activation.id) if action.activation is not None else None
                ),
            }
        )

    if active_channel_experiments(profile_id):
        return {
            "channel_profile_id": channel_profile_id,
            "started": False,
            "reason": "active_experiment_exists",
            "reconciled": reconciled,
        }

    for publication_id in channel_experiment_candidates(profile_id):
        action = start_packaging_experiment(publication_id)
        if action.action != "started" or action.experiment is None:
            continue
        if action.activation is None:
            raise RuntimeError("started packaging experiment has no activation")
        await start_packaging_activation_workflow(
            str(action.activation.id),
            action.activation.workflow_id,
        )
        return {
            "channel_profile_id": channel_profile_id,
            "started": True,
            "experiment_id": str(action.experiment.id),
            "activation_id": str(action.activation.id),
            "reason": action.reason,
            "reconciled": reconciled,
        }

    return {
        "channel_profile_id": channel_profile_id,
        "started": False,
        "reason": "no_eligible_test_recommendation",
        "reconciled": reconciled,
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
