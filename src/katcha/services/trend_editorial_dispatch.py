from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import select

from katcha.ai.subscription import subscription_connected
from katcha.config import get_ai_settings as get_settings
from katcha.db import session_scope
from katcha.orchestration.client import start_short_episode_editorial_workflow
from katcha.services.acquisition import assert_clip_production_eligible
from katcha.services.channel_economics import latest_economics_snapshot
from katcha.services.channel_profiles import ensure_active_profile
from katcha.short_episode_models import ShortEpisode, ShortEpisodeItem
from katcha.trend_activation_models import TrendActivationPolicyVersion

# Starting editorial can consume LLM/TTS credits. No existing planning-only
# policy silently becomes permission to spend. One operator-controlled policy
# switch, a recent budget snapshot, and live provider readiness are required.
_MAX_PER_SCAN = 2
_MAX_CANDIDATES_INSPECTED = 100
_MIN_HEADROOM_USD = Decimal("2.00")
_ECONOMICS_MAX_AGE = timedelta(hours=24)


def _editorial_dispatch_readiness(
    policy: TrendActivationPolicyVersion | None,
    *,
    ai_enabled: bool,
    provider_ready: bool,
    economics_age: timedelta | None,
    headroom: Decimal | None,
) -> str | None:
    if policy is None or not policy.enabled:
        return "activation_policy_disabled"
    if (policy.policy_metadata or {}).get("auto_start_editorial") is not True:
        return "editorial_auto_start_not_authorized"
    if not ai_enabled:
        return "ai_execution_disabled"
    if not provider_ready:
        return "ai_provider_unavailable"
    if economics_age is None or economics_age < timedelta(0):
        return "current_economics_unavailable"
    if economics_age > _ECONOMICS_MAX_AGE:
        return "economics_snapshot_stale"
    if headroom is None or headroom < max(
        Decimal(policy.min_budget_headroom_usd), _MIN_HEADROOM_USD
    ):
        return "insufficient_budget_headroom"
    return None


def _safe_limit(policy: TrendActivationPolicyVersion) -> int:
    value = (policy.policy_metadata or {}).get("editorial_max_starts_per_scan", 1)
    if type(value) is not int or not 1 <= value <= _MAX_PER_SCAN:
        return 1
    return value


async def dispatch_planned_trend_editorial(
    channel_profile_id: uuid.UUID,
    *,
    now: datetime | None = None,
) -> dict[str, object]:
    """Hand off planned, rights-qualified trend episodes to existing Temporal.

    Repeated scans reuse a deterministic workflow id and cannot create a
    second paid workflow for an episode. A scan never uploads or publishes.
    """
    reference = now or datetime.now(UTC)
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=UTC)
    settings = get_settings()
    provider_ready = bool(
        settings.openai_api_key
        or settings.gemini_api_key
        or subscription_connected(settings)
    )
    if settings.resolved_ai_execution_mode() == "fixture":
        provider_ready = settings.env != "production"

    with session_scope() as session:
        profile = ensure_active_profile(session, channel_profile_id)
        policy = session.scalar(
            select(TrendActivationPolicyVersion)
            .where(TrendActivationPolicyVersion.channel_profile_id == profile.id)
            .order_by(TrendActivationPolicyVersion.version.desc())
            .limit(1)
        )
        economics = latest_economics_snapshot(session, profile)
        sampled = economics.sampled_at if economics else None
        if sampled is not None and sampled.tzinfo is None:
            sampled = sampled.replace(tzinfo=UTC)
        age = reference - sampled if sampled else None
        budget = Decimal(economics.budget_headroom_usd) if economics else None
        reason = _editorial_dispatch_readiness(
            policy,
            ai_enabled=settings.ai_enabled,
            provider_ready=provider_ready,
            economics_age=age,
            headroom=budget,
        )
        if reason:
            return {
                "status": "blocked",
                "reason": reason,
                "started": [],
                "failed": [],
            }

        assert policy is not None
        dispatch_limit = _safe_limit(policy)
        candidates = list(
            session.scalars(
                select(ShortEpisode)
                .where(
                    ShortEpisode.channel_profile_id == channel_profile_id,
                    ShortEpisode.trend_opportunity_id.is_not(None),
                    ShortEpisode.parent_episode_id.is_(None),
                    ShortEpisode.status == "planned",
                    ShortEpisode.stage == "planned",
                )
                .order_by(ShortEpisode.created_at.desc(), ShortEpisode.id.desc())
                .limit(_MAX_CANDIDATES_INSPECTED)
            )
        )
        # Newsworthy episodes are time-sensitive. Repeatedly inspecting only
        # the oldest plans lets stale/rights-blocked rows starve fresh plans.
        # Inspect a bounded recent window, loading items in one query to keep
        # the larger window inexpensive and deterministic.
        items_by_episode: dict[uuid.UUID, list[ShortEpisodeItem]] = {}
        if candidates:
            for item in session.scalars(
                select(ShortEpisodeItem)
                .where(ShortEpisodeItem.short_episode_id.in_(
                    [episode.id for episode in candidates]
                ))
                .order_by(ShortEpisodeItem.position)
            ):
                items_by_episode.setdefault(item.short_episode_id, []).append(item)
        selected = []
        rejected = []
        for episode in candidates:
            items = items_by_episode.get(episode.id, [])
            if not items or any(
                not (item.acquisition_snapshot or {}).get("eligible")
                for item in items
            ):
                rejected.append({
                    "episode_id": str(episode.id),
                    "reason": "frozen_source_rights_not_eligible",
                })
                continue
            selected.append(
                (episode.id, episode.workflow_id, tuple(item.clip_id for item in items))
            )

    started: list[dict[str, str]] = []
    failed: list[dict[str, str]] = []
    dispatch_attempts = 0
    for episode_id, episode_workflow_id, clip_ids in selected:
        if dispatch_attempts >= dispatch_limit:
            break
        try:
            # Rights can change after planning. Revalidate against the current
            # acquisition ledger immediately before scheduling paid editorial.
            for clip_id in clip_ids:
                assert_clip_production_eligible(clip_id)
        except ValueError:
            failed.append({
                "episode_id": str(episode_id),
                "reason": "source_rights_changed_or_not_ready",
            })
            continue
        except Exception:
            # A ledger outage is not evidence that another candidate is safe.
            failed.append({
                "episode_id": str(episode_id),
                "reason": "source_eligibility_check_unavailable",
            })
            break
        # Count attempts, not successful replies: Temporal may have accepted a
        # start before the connection failed. Never backfill an ambiguous start
        # beyond the configured paid-work limit. Local eligibility rejection,
        # however, must not starve later ready candidates in the bounded window.
        dispatch_attempts += 1
        try:
            workflow_id = f"{episode_workflow_id}-editorial-script"
            await start_short_episode_editorial_workflow(
                str(episode_id), workflow_id, start_stage="script"
            )
            started.append({
                "episode_id": str(episode_id),
                "workflow_id": workflow_id,
            })
        except Exception:
            # The planned episode survives a Temporal outage and will be
            # retried on the next scan with the SAME workflow identity.
            failed.append({
                "episode_id": str(episode_id),
                "reason": "temporal_dispatch_unavailable",
            })
    return {
        "status": "dispatched" if started else "waiting" if not failed else "partial_failure",
        "started": started,
        "failed": failed,
        "skipped": rejected,
    }
