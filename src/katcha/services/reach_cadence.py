from __future__ import annotations

import uuid
from datetime import datetime
from zoneinfo import ZoneInfo

from katcha.db import session_scope
from katcha.domain import ChannelStatus, YouTubeConnectionStatus
from katcha.integrations.youtube.reporting import ANALYTICS_SCOPE, MONETARY_SCOPE
from katcha.intelligence_models import ChannelProfile
from katcha.publishing_models import YouTubeConnection

PACIFIC = ZoneInfo("America/Los_Angeles")


def reach_sync_identity(connection_id: uuid.UUID, at: datetime) -> str:
    if at.tzinfo is None:
        raise ValueError("reach sync cadence requires an aware timestamp")
    report_day = at.astimezone(PACIFIC).date().isoformat()
    return f"yt-reach-sync-daily-{connection_id}-{report_day}"


def eligible_reach_connection(channel_profile_id: uuid.UUID) -> tuple[uuid.UUID | None, str]:
    with session_scope() as session:
        channel = session.get(ChannelProfile, channel_profile_id)
        if channel is None:
            return None, "channel_not_found"
        if channel.status != ChannelStatus.ACTIVE.value:
            return None, "channel_inactive"
        connection = session.get(YouTubeConnection, channel.youtube_connection_id)
        if connection is None or connection.status != YouTubeConnectionStatus.ACTIVE.value:
            return None, "youtube_connection_inactive"
        if not ({ANALYTICS_SCOPE, MONETARY_SCOPE} & set(connection.scopes or [])):
            return None, "analytics_scope_missing"
        return connection.id, "eligible"
