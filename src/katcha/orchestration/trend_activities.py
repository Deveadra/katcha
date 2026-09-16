from __future__ import annotations

import uuid

from temporalio import activity

from katcha.services.trend_sources import poll_trend_source
from katcha.services.trends import refresh_channel_trends


@activity.defn
def refresh_channel_trends_activity(
    channel_profile_id: str,
    run_key: str,
) -> dict[str, object]:
    return refresh_channel_trends(uuid.UUID(channel_profile_id), run_key=run_key)


@activity.defn
def poll_trend_source_activity(
    source_id: str,
    run_key: str,
) -> dict[str, object]:
    return poll_trend_source(uuid.UUID(source_id), run_key=run_key)
