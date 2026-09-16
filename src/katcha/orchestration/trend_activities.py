from __future__ import annotations

import uuid

from temporalio import activity

from katcha.config import get_settings
from katcha.services.trend_source_health import source_health_summary
from katcha.services.trend_sources import poll_trend_source
from katcha.services.trends import refresh_channel_trends


@activity.defn
def refresh_channel_trends_activity(
    channel_profile_id: str,
    run_key: str,
) -> dict[str, object]:
    channel_id = uuid.UUID(channel_profile_id)
    settings = get_settings()
    health = source_health_summary(channel_id)
    configured = int(health.get("configured") or 0)
    coverage = float(health.get("coverage") or 0.0)
    if configured > 0 and coverage < settings.trend_min_source_coverage:
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
def poll_trend_source_activity(
    source_id: str,
    run_key: str,
) -> dict[str, object]:
    return poll_trend_source(uuid.UUID(source_id), run_key=run_key)
