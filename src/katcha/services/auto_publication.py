from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

from sqlalchemy import select

from katcha.config import get_settings
from katcha.db import session_scope
from katcha.domain import AutomationLevel, PublicationStatus
from katcha.intelligence_models import ChannelProfile
from katcha.models import DomainEvent
from katcha.production_models import Production, ProductionScript
from katcha.publishing_models import Publication, YouTubeConnection
from katcha.services.channel_automation import maybe_auto_demote
from katcha.services.channel_profiles import active_automation, ensure_active_profile
from katcha.services.channel_scheduling import (
    compute_schedule_recommendations,
    latest_schedule_recommendations,
)
from katcha.services.publications import (
    register_publication,
    register_short_episode_publication,
)
from katcha.short_episode_models import ShortEpisode, ShortEpisodeScript


AutoSourceKind = Literal["production", "short_episode"]


@dataclass(frozen=True, slots=True)
class AutoPublicationDecision:
    action: Literal["none", "private", "scheduled", "blocked"]
    reason: str
    publication: Publication | None = None


def _clean_title(value: str) -> str:
    title = " ".join(value.replace("<", "").replace(">", "").split()).strip()
    if not title:
        return "New short"
    if len(title) <= 100:
        return title
    shortened = title[:100].rstrip()
    if " " in shortened:
        shortened = shortened.rsplit(" ", 1)[0].rstrip()
    return shortened or title[:100]


def _production_title(session: object, production: Production) -> str:
    if production.selected_script_id is not None:
        script = session.get(ProductionScript, production.selected_script_id)
        if script is not None:
            title_angle = str((script.script_metadata or {}).get("title_angle") or "").strip()
            if title_angle:
                return _clean_title(title_angle)
    return _clean_title(
        str((production.analysis_snapshot or {}).get("transcript") or "New short")
    )


def _episode_title(session: object, episode: ShortEpisode) -> str:
    if episode.selected_script_id is not None:
        script = session.get(ShortEpisodeScript, episode.selected_script_id)
        if script is not None:
            title_angle = str((script.script_payload or {}).get("title_angle") or "").strip()
            if title_angle:
                return _clean_title(title_angle)
    return _clean_title(episode.premise)


def _next_occurrence(
    *,
    weekday: int,
    hour_local: int,
    timezone: ZoneInfo,
    now: datetime,
    extra_weeks: int = 0,
) -> datetime:
    local_now = now.astimezone(timezone)
    days = (weekday - local_now.weekday()) % 7
    candidate = (local_now + timedelta(days=days)).replace(
        hour=hour_local,
        minute=0,
        second=0,
        microsecond=0,
    )
    if candidate <= local_now + timedelta(minutes=15):
        candidate += timedelta(days=7)
    if extra_weeks:
        candidate += timedelta(days=7 * extra_weeks)
    return candidate.astimezone(UTC)


def _schedule_slot(
    profile: ChannelProfile,
    *,
    now: datetime,
) -> datetime:
    recommendations = latest_schedule_recommendations(profile.id, limit=5)
    if not recommendations:
        recommendations = compute_schedule_recommendations(
            profile.id,
            run_key=f"auto-publish-{now.date().isoformat()}",
            limit=5,
        )
    if not recommendations:
        raise ValueError("channel has no usable publication schedule")

    timezone = ZoneInfo(profile.timezone)
    with session_scope() as session:
        for week in range(8):
            for item in recommendations:
                candidate = _next_occurrence(
                    weekday=item.weekday,
                    hour_local=item.hour_local,
                    timezone=timezone,
                    now=now,
                    extra_weeks=week,
                )
                occupied = session.scalar(
                    select(Publication.id).where(
                        Publication.youtube_connection_id == profile.youtube_connection_id,
                        Publication.publish_at == candidate,
                        Publication.status != PublicationStatus.FAILED.value,
                    )
                )
                if occupied is None:
                    return candidate
    raise ValueError("no unoccupied publication slot is available in the next eight weeks")


def _record_blocked(
    channel_profile_id: uuid.UUID,
    *,
    source_kind: AutoSourceKind,
    source_id: uuid.UUID,
    reason: str,
) -> None:
    with session_scope() as session:
        session.add(
            DomainEvent(
                aggregate_type=source_kind,
                aggregate_id=str(source_id),
                event_type=f"{source_kind}.auto_publication_blocked",
                payload={
                    "channel_profile_id": str(channel_profile_id),
                    "source_kind": source_kind,
                    "source_id": str(source_id),
                    "reason": reason,
                },
            )
        )


def prepare_auto_publication(
    source_kind: AutoSourceKind,
    source_id: uuid.UUID,
    *,
    now: datetime | None = None,
) -> AutoPublicationDecision:
    now = (now or datetime.now(UTC)).astimezone(UTC)
    settings = get_settings()

    with session_scope() as session:
        if source_kind == "production":
            source = session.get(Production, source_id)
        else:
            source = session.get(ShortEpisode, source_id)
        if source is None:
            raise ValueError(f"{source_kind.replace('_', ' ')} not found: {source_id}")
        profile_id = source.channel_profile_id
        if profile_id is None:
            return AutoPublicationDecision("none", "source_is_not_channel_scoped")
        profile = ensure_active_profile(session, profile_id)
        session.expunge(profile)

    maybe_auto_demote(profile_id)

    with session_scope() as session:
        profile = ensure_active_profile(session, profile_id)
        policy = active_automation(session, profile)
        level = AutomationLevel(policy.level)
        connection = session.get(YouTubeConnection, profile.youtube_connection_id)
        if connection is None or connection.status != "active":
            _record_blocked(
                profile.id,
                source_kind=source_kind,
                source_id=source_id,
                reason="youtube_connection_inactive",
            )
            return AutoPublicationDecision("blocked", "youtube_connection_inactive")
        if level not in {
            AutomationLevel.AUTO_PUBLISH_PRIVATE,
            AutomationLevel.AUTO_PUBLISH_SCHEDULED,
        }:
            return AutoPublicationDecision("none", f"automation_level_{level.value}")

        if source_kind == "production":
            source = session.get(Production, source_id)
            if source is None:
                raise ValueError(f"production not found: {source_id}")
            title = _production_title(session, source)
        else:
            source = session.get(ShortEpisode, source_id)
            if source is None:
                raise ValueError(f"short episode not found: {source_id}")
            title = _episode_title(session, source)

        profile_metadata = dict(profile.profile_metadata or {})
        tags = [
            str(value).strip()
            for value in list(profile_metadata.get("publication_tags") or [])
            if str(value).strip()
        ]
        description = str(profile_metadata.get("publication_description") or "").strip()
        category_id = str(profile_metadata.get("youtube_category_id") or "").strip() or None
        notify_subscribers = bool(profile_metadata.get("notify_subscribers", False))
        made_for_kids = bool(profile_metadata.get("made_for_kids", False))
        contains_synthetic_media = bool(
            profile_metadata.get("contains_synthetic_media", False)
        )
        youtube_connection_id = profile.youtube_connection_id
        session.expunge(profile)

    if not settings.youtube_client_id or not settings.youtube_client_secret:
        _record_blocked(
            profile_id,
            source_kind=source_kind,
            source_id=source_id,
            reason="youtube_oauth_client_unconfigured",
        )
        return AutoPublicationDecision("blocked", "youtube_oauth_client_unconfigured")
    if not settings.credential_encryption_key:
        _record_blocked(
            profile_id,
            source_kind=source_kind,
            source_id=source_id,
            reason="credential_encryption_unconfigured",
        )
        return AutoPublicationDecision("blocked", "credential_encryption_unconfigured")

    publish_at = None
    privacy_status = "private"
    action: Literal["private", "scheduled"] = "private"
    if level == AutomationLevel.AUTO_PUBLISH_SCHEDULED:
        with session_scope() as session:
            profile = ensure_active_profile(session, profile_id)
            session.expunge(profile)
        try:
            publish_at = _schedule_slot(profile, now=now)
        except Exception as exc:
            _record_blocked(
                profile_id,
                source_kind=source_kind,
                source_id=source_id,
                reason=str(exc),
            )
            return AutoPublicationDecision("blocked", str(exc))
        privacy_status = "public"
        action = "scheduled"

    register = (
        register_publication
        if source_kind == "production"
        else register_short_episode_publication
    )
    try:
        publication = register(
            source_id,
            youtube_connection_id=youtube_connection_id,
            title=title,
            description=description,
            tags=tags,
            category_id=category_id,
            privacy_status=privacy_status,
            publish_at=publish_at,
            notify_subscribers=notify_subscribers,
            made_for_kids=made_for_kids,
            contains_synthetic_media=contains_synthetic_media,
        )
    except ValueError as exc:
        _record_blocked(
            profile_id,
            source_kind=source_kind,
            source_id=source_id,
            reason=str(exc),
        )
        return AutoPublicationDecision("blocked", str(exc))

    with session_scope() as session:
        session.add(
            DomainEvent(
                aggregate_type=source_kind,
                aggregate_id=str(source_id),
                event_type=f"{source_kind}.auto_publication_registered",
                payload={
                    "channel_profile_id": str(profile_id),
                    "source_kind": source_kind,
                    "source_id": str(source_id),
                    "publication_id": str(publication.id),
                    "automation_level": level.value,
                    "action": action,
                    "publish_at": publish_at.isoformat() if publish_at else None,
                },
            )
        )
    return AutoPublicationDecision(action, "registered", publication)
