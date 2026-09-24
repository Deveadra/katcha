from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from katcha.db import session_scope
from katcha.integrations.youtube.reporting import ANALYTICS_SCOPE, MONETARY_SCOPE
from katcha.intelligence_models import ChannelProfile
from katcha.publishing_models import YouTubeConnection

PACIFIC = ZoneInfo("America/Los_Angeles")


@dataclass(frozen=True, slots=True)
class AutomaticReachSyncRequest:
    channel_profile_id: uuid.UUID
    connection_id: uuid.UUID | None
    workflow_id: str | None
    report_day: str
    should_start: bool
    reason: str


def automatic_reach_sync_request(
    channel_profile_id: uuid.UUID,
    *,
    now: datetime | None = None,
) -> AutomaticReachSyncRequest:
    current = now or datetime.now(UTC)
    if current.tzinfo is None:
        current = current.replace(tzinfo=UTC)
    report_day = current.astimezone(PACIFIC).date().isoformat()

    with session_scope() as session:
        profile = session.get(ChannelProfile, channel_profile_id)
        if profile is None:
            raise ValueError(f"channel profile not found: {channel_profile_id}")
        connection = session.get(YouTubeConnection, profile.youtube_connection_id)
        if connection is None:
            raise ValueError("channel profile references a missing YouTube connection")
        scopes = set(connection.scopes or [])
        if ANALYTICS_SCOPE not in scopes and MONETARY_SCOPE not in scopes:
            return AutomaticReachSyncRequest(
                channel_profile_id=channel_profile_id,
                connection_id=connection.id,
                workflow_id=None,
                report_day=report_day,
                should_start=False,
                reason="analytics_scope_unavailable",
            )
        workflow_id = f"yt-reach-auto-{connection.id}-{report_day}"
        return AutomaticReachSyncRequest(
            channel_profile_id=channel_profile_id,
            connection_id=connection.id,
            workflow_id=workflow_id,
            report_day=report_day,
            should_start=True,
            reason="daily_sync_due",
        )
