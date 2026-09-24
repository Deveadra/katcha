from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

from sqlalchemy import select

from katcha.config import get_settings
from katcha.db import session_scope
from katcha.domain import AutomationLevel, ProductionStatus, ReviewDecision
from katcha.intelligence_models import (
    AutomationPolicyVersion,
    ChannelProfile,
    ChannelStrategyVersion,
    ScheduleRecommendation,
)
from katcha.models import DomainEvent
from katcha.production_models import Production, ProductionAsset, ProductionScript
from katcha.publishing_models import Publication
from katcha.services.channel_profiles import active_automation, active_strategy
from katcha.services.productions import review_production
from katcha.services.publications import (
    register_publication,
    register_short_episode_publication,
)
from katcha.services.short_episode_reviews import review_short_episode
from katcha.short_episode_models import ShortEpisode, ShortEpisodeAsset, ShortEpisodeItem

AutomationSourceKind = Literal["production", "short_episode"]
_AUTO_APPROVE_LEVELS = {
    AutomationLevel.AUTO_APPROVE_LOW_RISK,
    AutomationLevel.AUTO_PUBLISH_PRIVATE,
    AutomationLevel.AUTO_PUBLISH_SCHEDULED,
}
_AUTO_PUBLISH_LEVELS = {
    AutomationLevel.AUTO_PUBLISH_PRIVATE,
    AutomationLevel.AUTO_PUBLISH_SCHEDULED,
}


@dataclass(frozen=True, slots=True)
class RenderAutomationResult:
    source_kind: AutomationSourceKind
    source_id: str
    action: str
    automation_level: str | None
    reason: str
    publication_id: str | None = None
    publication_workflow_id: str | None = None
    publish_at: str | None = None

    def payload(self) -> dict[str, object]:
        return asdict(self)


def _profile_policy_strategy(
    session: object,
    channel_profile_id: uuid.UUID,
) -> tuple[ChannelProfile, AutomationPolicyVersion, ChannelStrategyVersion]:
    profile = session.get(ChannelProfile, channel_profile_id)
    if profile is None:
        raise RuntimeError("channel-scoped source references a missing profile")
    policy = active_automation(session, profile)
    strategy = active_strategy(session, profile)
    return profile, policy, strategy


def _green_snapshot(snapshot: dict[str, object]) -> bool:
    return (
        snapshot.get("eligible") is True
        and str(snapshot.get("rights_lane") or "").lower() == "green"
    )


def _production_low_risk(production: Production) -> bool:
    acquisition = dict((production.analysis_snapshot or {}).get("acquisition") or {})
    return _green_snapshot(acquisition)


def _episode_low_risk(session: object, episode: ShortEpisode) -> bool:
    items = list(
        session.scalars(
            select(ShortEpisodeItem).where(
                ShortEpisodeItem.short_episode_id == episode.id
            )
        )
    )
    return (
        len(items) == episode.item_count
        and bool(items)
        and all(_green_snapshot(dict(item.acquisition_snapshot or {})) for item in items)
    )


def _verified_render(
    session: object,
    source_kind: AutomationSourceKind,
    source_id: uuid.UUID,
) -> bool:
    if source_kind == "production":
        asset = session.scalar(
            select(ProductionAsset).where(
                ProductionAsset.production_id == source_id,
                ProductionAsset.kind == "render",
                ProductionAsset.generation == 1,
            )
        )
    else:
        asset = session.scalar(
            select(ShortEpisodeAsset).where(
                ShortEpisodeAsset.short_episode_id == source_id,
                ShortEpisodeAsset.kind == "render",
                ShortEpisodeAsset.generation == 1,
            )
        )
    return asset is not None and bool((asset.asset_metadata or {}).get("verified"))


def _slot_tuple(raw: dict[str, object]) -> tuple[int, int] | None:
    try:
        weekday = int(raw["weekday"])
        hour_local = int(raw["hour_local"])
    except (KeyError, TypeError, ValueError):
        return None
    if not 0 <= weekday <= 6 or not 0 <= hour_local <= 23:
        return None
    return weekday, hour_local


def _schedule_slots(
    session: object,
    profile: ChannelProfile,
    strategy: ChannelStrategyVersion,
) -> list[tuple[int, int]]:
    latest = session.scalar(
        select(ScheduleRecommendation)
        .where(ScheduleRecommendation.channel_profile_id == profile.id)
        .order_by(ScheduleRecommendation.created_at.desc())
        .limit(1)
    )
    raw_slots: list[dict[str, object]]
    if latest is not None:
        recommendations = list(
            session.scalars(
                select(ScheduleRecommendation)
                .where(
                    ScheduleRecommendation.channel_profile_id == profile.id,
                    ScheduleRecommendation.run_key == latest.run_key,
                )
                .order_by(ScheduleRecommendation.rank)
            )
        )
        raw_slots = [
            {"weekday": item.weekday, "hour_local": item.hour_local}
            for item in recommendations
        ]
    else:
        raw_slots = list(strategy.fallback_schedule or [])

    slots: list[tuple[int, int]] = []
    seen: set[tuple[int, int]] = set()
    for raw in raw_slots:
        parsed = _slot_tuple(raw)
        if parsed is None or parsed in seen:
            continue
        seen.add(parsed)
        slots.append(parsed)
    return slots


def _next_slot_candidates(
    *,
    now: datetime,
    timezone: str,
    slots: list[tuple[int, int]],
    blackouts: set[tuple[int, int]],
    weeks: int = 8,
) -> list[datetime]:
    zone = ZoneInfo(timezone)
    local_now = now.astimezone(zone)
    earliest = local_now + timedelta(minutes=15)
    candidates: list[datetime] = []
    for weekday, hour_local in slots:
        if (weekday, hour_local) in blackouts:
            continue
        days = (weekday - local_now.weekday()) % 7
        candidate = (local_now + timedelta(days=days)).replace(
            hour=hour_local,
            minute=0,
            second=0,
            microsecond=0,
        )
        if candidate <= earliest:
            candidate += timedelta(days=7)
        for week in range(weeks):
            candidates.append((candidate + timedelta(days=7 * week)).astimezone(UTC))
    return sorted(set(candidates))


def _next_publish_at(
    session: object,
    profile: ChannelProfile,
    strategy: ChannelStrategyVersion,
    *,
    now: datetime | None = None,
) -> datetime:
    slots = _schedule_slots(session, profile, strategy)
    if not slots:
        raise ValueError("scheduled automation has no recommended or fallback publish window")
    blackouts = {
        parsed
        for raw in list(strategy.blackout_windows or [])
        if (parsed := _slot_tuple(raw)) is not None
    }
    candidates = _next_slot_candidates(
        now=now or datetime.now(UTC),
        timezone=profile.timezone,
        slots=slots,
        blackouts=blackouts,
    )
    if not candidates:
        raise ValueError("all configured publish windows are blacked out")

    occupied = set(
        session.scalars(
            select(Publication.publish_at).where(
                Publication.youtube_connection_id == profile.youtube_connection_id,
                Publication.publish_at.is_not(None),
                Publication.publish_at >= datetime.now(UTC),
            )
        )
    )
    for candidate in candidates:
        if candidate not in occupied:
            return candidate
    raise ValueError("no collision-free publish window is available")


def _publication_defaults(strategy: ChannelStrategyVersion) -> dict[str, object]:
    routing = dict(strategy.routing_policy or {})
    defaults = routing.get("publication_defaults")
    return dict(defaults) if isinstance(defaults, dict) else {}


def _production_title(session: object, production: Production) -> str:
    if production.selected_script_id is None:
        raise ValueError("approved production has no selected script")
    script = session.get(ProductionScript, production.selected_script_id)
    if script is None:
        raise ValueError("approved production selected script is missing")
    title = str((script.script_metadata or {}).get("title_angle") or "").strip()
    if not title:
        title = script.narration.strip()
    if not title:
        raise ValueError("approved production has no usable publication title")
    return title[:100].strip()


def _episode_title(episode: ShortEpisode) -> str:
    title = episode.premise.strip()
    if not title:
        raise ValueError("approved short episode has no usable publication title")
    return title[:100].strip()


def _metadata(
    defaults: dict[str, object],
) -> tuple[str, list[str], str | None, bool, bool, bool]:
    description = str(defaults.get("description") or "").strip()
    raw_tags = defaults.get("tags")
    tags = (
        [str(tag).strip() for tag in raw_tags if str(tag).strip()]
        if isinstance(raw_tags, list)
        else []
    )
    category_id = str(defaults.get("category_id") or "").strip() or None
    notify_subscribers = bool(defaults.get("notify_subscribers", False))
    made_for_kids = bool(defaults.get("made_for_kids", False))
    contains_synthetic_media = bool(defaults.get("contains_synthetic_media", False))
    return (
        description,
        tags,
        category_id,
        notify_subscribers,
        made_for_kids,
        contains_synthetic_media,
    )


def _emit_result(result: RenderAutomationResult) -> None:
    with session_scope() as session:
        session.add(
            DomainEvent(
                aggregate_type=result.source_kind,
                aggregate_id=result.source_id,
                event_type=f"{result.source_kind}.render_automation_{result.action}",
                payload=result.payload(),
            )
        )


def advance_short_episode_editorial_automation(
    episode_id: uuid.UUID,
) -> RenderAutomationResult:
    with session_scope() as session:
        episode = session.get(ShortEpisode, episode_id)
        if episode is None:
            raise ValueError(f"short episode not found: {episode_id}")
        profile, policy, _ = _profile_policy_strategy(
            session, episode.channel_profile_id
        )
        level = AutomationLevel(policy.level)
        if episode.status == "editorial_approved":
            result = RenderAutomationResult(
                source_kind="short_episode",
                source_id=str(episode.id),
                action="editorial_approved",
                automation_level=level.value,
                reason="editorial approval already exists",
            )
            _emit_result(result)
            return result
        if level not in _AUTO_APPROVE_LEVELS:
            result = RenderAutomationResult(
                source_kind="short_episode",
                source_id=str(episode.id),
                action="editorial_review_required",
                automation_level=level.value,
                reason="channel automation policy requires editorial review",
            )
            _emit_result(result)
            return result
        if not _episode_low_risk(session, episode):
            result = RenderAutomationResult(
                source_kind="short_episode",
                source_id=str(episode.id),
                action="editorial_review_required",
                automation_level=level.value,
                reason="one or more episode items are not green-lane production eligible",
            )
            _emit_result(result)
            return result
        if episode.status not in {"voiced", "review"}:
            raise ValueError("short episode is not awaiting editorial review")

    review_short_episode(
        episode_id,
        decision=ReviewDecision.APPROVE,
        note="Auto-approved by channel automation after green-lane editorial checks",
        actor="system:render-automation",
    )
    result = RenderAutomationResult(
        source_kind="short_episode",
        source_id=str(episode_id),
        action="editorial_approved",
        automation_level=level.value,
        reason="green-lane episode passed channel auto-approval policy",
    )
    _emit_result(result)
    return result


def advance_render_automation(
    source_kind: AutomationSourceKind,
    source_id: uuid.UUID,
) -> RenderAutomationResult:
    with session_scope() as session:
        if source_kind == "production":
            source = session.get(Production, source_id)
        else:
            source = session.get(ShortEpisode, source_id)
        if source is None:
            raise ValueError(f"{source_kind.replace('_', ' ')} not found: {source_id}")
        if source.channel_profile_id is None:
            result = RenderAutomationResult(
                source_kind=source_kind,
                source_id=str(source_id),
                action="review_required",
                automation_level=None,
                reason="unscoped content cannot use channel automation",
            )
            _emit_result(result)
            return result

        profile, policy, strategy = _profile_policy_strategy(
            session, source.channel_profile_id
        )
        level = AutomationLevel(policy.level)
        if not _verified_render(session, source_kind, source_id):
            raise ValueError("render automation requires a verified render asset")
        low_risk = (
            _production_low_risk(source)
            if source_kind == "production"
            else _episode_low_risk(session, source)
        )
        approved = (
            source.status == ProductionStatus.APPROVED.value
            if source_kind == "production"
            else source.status == "approved" and source.stage == "render_approved"
        )
        if not approved and level not in _AUTO_APPROVE_LEVELS:
            result = RenderAutomationResult(
                source_kind=source_kind,
                source_id=str(source_id),
                action="review_required",
                automation_level=level.value,
                reason="channel automation policy requires render review",
            )
            _emit_result(result)
            return result
        if not approved and not low_risk:
            result = RenderAutomationResult(
                source_kind=source_kind,
                source_id=str(source_id),
                action="review_required",
                automation_level=level.value,
                reason="source rights/readiness is not green-lane",
            )
            _emit_result(result)
            return result

    if not approved:
        if source_kind == "production":
            review_production(
                source_id,
                decision=ReviewDecision.APPROVE,
                note="Auto-approved verified green-lane render",
                actor="system:render-automation",
            )
        else:
            review_short_episode(
                source_id,
                decision=ReviewDecision.APPROVE,
                note="Auto-approved verified green-lane render",
                actor="system:render-automation",
            )

    if level not in _AUTO_PUBLISH_LEVELS:
        result = RenderAutomationResult(
            source_kind=source_kind,
            source_id=str(source_id),
            action="approved",
            automation_level=level.value,
            reason="render approved; channel policy does not auto-publish",
        )
        _emit_result(result)
        return result

    settings = get_settings()
    if (
        not settings.youtube_client_id
        or not settings.youtube_client_secret
        or not settings.credential_encryption_key
    ):
        result = RenderAutomationResult(
            source_kind=source_kind,
            source_id=str(source_id),
            action="publish_blocked",
            automation_level=level.value,
            reason="YouTube credentials are not configured",
        )
        _emit_result(result)
        return result

    with session_scope() as session:
        if source_kind == "production":
            source = session.get(Production, source_id)
        else:
            source = session.get(ShortEpisode, source_id)
        if source is None or source.channel_profile_id is None:
            raise RuntimeError("approved source lost its channel scope")
        profile, _, strategy = _profile_policy_strategy(
            session, source.channel_profile_id
        )
        defaults = _publication_defaults(strategy)
        (
            description,
            tags,
            category_id,
            notify_subscribers,
            made_for_kids,
            contains_synthetic_media,
        ) = _metadata(defaults)
        title = (
            _production_title(session, source)
            if source_kind == "production"
            else _episode_title(source)
        )
        if level == AutomationLevel.AUTO_PUBLISH_SCHEDULED:
            try:
                publish_at = _next_publish_at(session, profile, strategy)
            except ValueError as exc:
                result = RenderAutomationResult(
                    source_kind=source_kind,
                    source_id=str(source_id),
                    action="publish_blocked",
                    automation_level=level.value,
                    reason=str(exc),
                )
                _emit_result(result)
                return result
            privacy_status = "public"
        else:
            publish_at = None
            privacy_status = "private"
        youtube_connection_id = profile.youtube_connection_id

    if source_kind == "production":
        publication = register_publication(
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
    else:
        publication = register_short_episode_publication(
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

    result = RenderAutomationResult(
        source_kind=source_kind,
        source_id=str(source_id),
        action="publication_queued",
        automation_level=level.value,
        reason="verified approved render handed to existing publication ledger",
        publication_id=str(publication.id),
        publication_workflow_id=publication.workflow_id,
        publish_at=publish_at.isoformat() if publish_at else None,
    )
    _emit_result(result)
    return result
