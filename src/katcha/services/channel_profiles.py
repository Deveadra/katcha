from __future__ import annotations

import uuid
from decimal import Decimal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select
from sqlalchemy.orm import Session

from katcha.config import get_settings
from katcha.db import session_scope
from katcha.domain import AutomationLevel, ChannelStatus
from katcha.intelligence_models import (
    AutomationPolicyVersion,
    ChannelProfile,
    ChannelStrategyVersion,
)
from katcha.models import DomainEvent
from katcha.publishing_models import YouTubeConnection


def validate_timezone(value: str) -> str:
    candidate = value.strip()
    if not candidate:
        raise ValueError("channel timezone cannot be empty")
    try:
        ZoneInfo(candidate)
    except ZoneInfoNotFoundError as exc:
        raise ValueError(f"unknown IANA timezone: {candidate}") from exc
    return candidate


def validate_schedule(items: list[dict[str, object]]) -> list[dict[str, int]]:
    result: list[dict[str, int]] = []
    seen: set[tuple[int, int]] = set()
    for raw in items:
        try:
            weekday = int(raw["weekday"])
            hour_local = int(raw["hour_local"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(
                "schedule entries require integer weekday and hour_local"
            ) from exc
        if not 0 <= weekday <= 6 or not 0 <= hour_local <= 23:
            raise ValueError("schedule weekday/hour are outside valid ranges")
        key = (weekday, hour_local)
        if key in seen:
            continue
        seen.add(key)
        result.append({"weekday": weekday, "hour_local": hour_local})
    return result


def active_strategy(
    session: Session,
    profile: ChannelProfile,
) -> ChannelStrategyVersion:
    strategy = session.scalar(
        select(ChannelStrategyVersion).where(
            ChannelStrategyVersion.channel_profile_id == profile.id,
            ChannelStrategyVersion.version == profile.active_strategy_version,
        )
    )
    if strategy is None:
        raise RuntimeError("channel profile has no active strategy version")
    return strategy


def active_automation(
    session: Session,
    profile: ChannelProfile,
) -> AutomationPolicyVersion:
    policy = session.scalar(
        select(AutomationPolicyVersion).where(
            AutomationPolicyVersion.channel_profile_id == profile.id,
            AutomationPolicyVersion.version == profile.active_automation_version,
        )
    )
    if policy is None:
        raise RuntimeError("channel profile has no active automation policy")
    return policy


def ensure_active_profile(
    session: Session,
    channel_profile_id: uuid.UUID,
) -> ChannelProfile:
    profile = session.get(ChannelProfile, channel_profile_id)
    if profile is None:
        raise ValueError(f"channel profile not found: {channel_profile_id}")
    if profile.status != ChannelStatus.ACTIVE.value:
        raise ValueError("channel profile is not active")
    return profile


def ensure_channel_profile(
    youtube_connection_id: uuid.UUID,
    *,
    timezone: str = "UTC",
    fallback_schedule: list[dict[str, object]] | None = None,
) -> ChannelProfile:
    timezone = validate_timezone(timezone)
    fallback = validate_schedule(fallback_schedule or [])
    settings = get_settings()
    with session_scope() as session:
        existing = session.scalar(
            select(ChannelProfile).where(
                ChannelProfile.youtube_connection_id == youtube_connection_id
            )
        )
        if existing is not None:
            session.expunge(existing)
            return existing
        connection = session.get(YouTubeConnection, youtube_connection_id)
        if connection is None:
            raise ValueError(f"YouTube connection not found: {youtube_connection_id}")

        profile = ChannelProfile(
            youtube_connection_id=youtube_connection_id,
            status=ChannelStatus.ACTIVE.value,
            timezone=timezone,
            active_strategy_version=1,
            active_automation_version=1,
            profile_metadata={"channel_id": connection.channel_id},
        )
        session.add(profile)
        session.flush()
        session.add(
            ChannelStrategyVersion(
                channel_profile_id=profile.id,
                version=1,
                monthly_hard_budget_usd=Decimal(
                    str(settings.ai_budget_usd_monthly)
                ),
                reinvestment_rate=Decimal("1"),
                reinvestment_cap_usd=Decimal("100"),
                fallback_schedule=fallback,
                blackout_windows=[],
                routing_policy={
                    "mode": "balanced",
                    "quality_floor": "task_default",
                },
                strategy_metadata={"created_by": "profile_bootstrap"},
            )
        )
        session.add(
            AutomationPolicyVersion(
                channel_profile_id=profile.id,
                version=1,
                level=AutomationLevel.REVIEW_REQUIRED.value,
                policy_metadata={"created_by": "profile_bootstrap"},
            )
        )
        session.add(
            DomainEvent(
                aggregate_type="channel_profile",
                aggregate_id=str(profile.id),
                event_type="channel_profile.created",
                payload={
                    "channel_profile_id": str(profile.id),
                    "youtube_connection_id": str(youtube_connection_id),
                    "timezone": timezone,
                    "automation_level": AutomationLevel.REVIEW_REQUIRED.value,
                },
            )
        )
        session.refresh(profile)
        session.expunge(profile)
        return profile


def create_strategy_version(
    channel_profile_id: uuid.UUID,
    *,
    monthly_hard_budget_usd: Decimal | None = None,
    reinvestment_rate: Decimal | None = None,
    reinvestment_cap_usd: Decimal | None = None,
    fallback_schedule: list[dict[str, object]] | None = None,
    blackout_windows: list[dict[str, object]] | None = None,
    routing_policy: dict[str, object] | None = None,
    actor: str = "operator",
) -> ChannelStrategyVersion:
    with session_scope() as session:
        profile = ensure_active_profile(session, channel_profile_id)
        current = active_strategy(session, profile)
        budget = (
            monthly_hard_budget_usd
            if monthly_hard_budget_usd is not None
            else current.monthly_hard_budget_usd
        )
        rate = (
            reinvestment_rate
            if reinvestment_rate is not None
            else current.reinvestment_rate
        )
        cap = (
            reinvestment_cap_usd
            if reinvestment_cap_usd is not None
            else current.reinvestment_cap_usd
        )
        if budget < 0 or cap < 0:
            raise ValueError("channel budgets cannot be negative")
        if rate < 0 or rate > 1:
            raise ValueError("reinvestment_rate must be between 0 and 1")
        fallback = (
            validate_schedule(fallback_schedule)
            if fallback_schedule is not None
            else list(current.fallback_schedule or [])
        )
        blackouts = (
            validate_schedule(blackout_windows)
            if blackout_windows is not None
            else list(current.blackout_windows or [])
        )
        version = profile.active_strategy_version + 1
        strategy = ChannelStrategyVersion(
            channel_profile_id=profile.id,
            version=version,
            monthly_hard_budget_usd=budget,
            reinvestment_rate=rate,
            reinvestment_cap_usd=cap,
            fallback_schedule=fallback,
            blackout_windows=blackouts,
            routing_policy=dict(
                routing_policy
                if routing_policy is not None
                else current.routing_policy or {}
            ),
            strategy_metadata={"actor": actor, "supersedes": current.version},
        )
        session.add(strategy)
        profile.active_strategy_version = version
        session.add(
            DomainEvent(
                aggregate_type="channel_profile",
                aggregate_id=str(profile.id),
                event_type="channel_profile.strategy_updated",
                payload={
                    "channel_profile_id": str(profile.id),
                    "strategy_version": version,
                    "actor": actor,
                },
            )
        )
        session.flush()
        session.refresh(strategy)
        session.expunge(strategy)
        return strategy
