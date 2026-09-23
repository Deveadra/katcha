from __future__ import annotations

import uuid

from temporalio import activity

from katcha.config import get_settings
from katcha.services.trend_calibration import refresh_trend_calibration
from katcha.services.trend_source_reliability import (
    channel_trend_source_health,
    source_health_allows_refresh,
)
from katcha.services.trends import refresh_channel_trends


@activity.defn
def refresh_channel_trends_activity(
    channel_profile_id: str,
    run_key: str,
) -> dict[str, object]:
    channel_id = uuid.UUID(channel_profile_id)
    settings = get_settings()
    health = channel_trend_source_health(channel_id)
    if not source_health_allows_refresh(
        health,
        minimum_coverage=settings.trend_min_source_coverage,
    ):
        return {
            "channel_profile_id": channel_profile_id,
            "run_key": run_key,
            "status": "withheld_source_coverage",
            "source_health": health,
            "minimum_source_coverage": settings.trend_min_source_coverage,
            "topics_scored": 0,
            "qualified_opportunities": 0,
            "evidence_packets_built": 0,
        }
    result = refresh_channel_trends(channel_id, run_key=run_key)
    return {
        **result,
        "status": "completed",
        "source_health": health,
    }


@activity.defn
def refresh_trend_calibration_activity(
    channel_profile_id: str,
    run_key: str,
    target_age_hours: int,
) -> dict[str, object]:
    snapshot = refresh_trend_calibration(
        uuid.UUID(channel_profile_id),
        run_key=run_key,
        target_age_hours=target_age_hours,
    )
    return {
        "channel_profile_id": channel_profile_id,
        "calibration_snapshot_id": str(snapshot.id),
        "version": snapshot.version,
        "run_key": snapshot.run_key,
        "status": snapshot.status,
        "sample_count": snapshot.sample_count,
        "validation_sample_count": snapshot.validation_sample_count,
        "blend_ratio": float(snapshot.blend_ratio),
        "confidence": float(snapshot.confidence),
    }
