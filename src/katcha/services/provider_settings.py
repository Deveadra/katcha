from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select

from katcha.db import session_scope
from katcha.intelligence_models import ChannelProfile
from katcha.models import DomainEvent
from katcha.provider_setting_models import ChannelProviderSetting


def get_channel_provider_setting(
    channel_profile_id: uuid.UUID,
    provider: str,
) -> ChannelProviderSetting | None:
    with session_scope() as session:
        row = session.scalar(
            select(ChannelProviderSetting).where(
                ChannelProviderSetting.channel_profile_id == channel_profile_id,
                ChannelProviderSetting.provider == provider,
            )
        )
        if row is not None:
            session.expunge(row)
        return row


def upsert_channel_provider_setting(
    channel_profile_id: uuid.UUID,
    *,
    provider: str,
    enabled: bool,
    config: dict[str, Any],
    actor: str = "operator",
) -> ChannelProviderSetting:
    provider = provider.strip().lower()
    if provider not in {"elevenlabs", "invideo"}:
        raise ValueError(f"unsupported channel provider: {provider}")

    with session_scope() as session:
        if session.get(ChannelProfile, channel_profile_id) is None:
            raise ValueError("channel profile not found")
        row = session.scalar(
            select(ChannelProviderSetting).where(
                ChannelProviderSetting.channel_profile_id == channel_profile_id,
                ChannelProviderSetting.provider == provider,
            )
        )
        if row is None:
            row = ChannelProviderSetting(
                channel_profile_id=channel_profile_id,
                provider=provider,
            )
            session.add(row)
        row.enabled = enabled
        row.config = dict(config or {})
        session.flush()
        session.add(
            DomainEvent(
                aggregate_type="channel_profile",
                aggregate_id=str(channel_profile_id),
                event_type="channel_profile.provider_updated",
                payload={
                    "channel_profile_id": str(channel_profile_id),
                    "provider": provider,
                    "enabled": enabled,
                    "config_keys": sorted(row.config),
                    "actor": actor,
                },
            )
        )
        session.refresh(row)
        session.expunge(row)
        return row
