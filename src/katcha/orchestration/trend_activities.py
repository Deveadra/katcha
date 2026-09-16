from __future__ import annotations

import uuid

from temporalio import activity

from katcha.services.trends import refresh_channel_trends


@activity.defn
def refresh_channel_trends_activity(
    channel_profile_id: str,
    run_key: str,
) -> dict[str, object]:
    return refresh_channel_trends(uuid.UUID(channel_profile_id), run_key=run_key)
