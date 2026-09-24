from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from sqlalchemy import func, select

from katcha.db import session_scope
from katcha.intelligence_models import ChannelProfile
from katcha.models import DomainEvent
from katcha.opportunity_activation_models import (
    OpportunityActivationDecision,
    OpportunityActivationPolicyVersion,
    OpportunityActivationRun,
)
from katcha.services.channel_economics import budget_state
from katcha.services.channel_profiles import ensure_active_profile
from katcha.services.trend_activation import (
    activate_trend_opportunity,
    preview_trend_activation,
)
from katcha.short_episode_models import ShortEpisode
from katcha.trend_models import TrendOpportunity

_TERMINAL_EPISODE_STATUSES = {"approved", "rejected", "failed"}


@dataclass(frozen=True, slots=True)
class ActivationRunResult:
    run_id: uuid.UUID
    channel_profile_id: uuid.UUID
    run_key: str
    policy_version: int
    status: str
    scanned_count: int
    activated_count: int
    skipped_count: int


def _utc(value: datetime | None = None) -> datetime:
    current = value or datetime.now(UTC)
    if current.tzinfo is None:
        return current.replace(tzinfo=UTC)
    return current.astimezone(UTC)


def ensure_activation_policy(
    channel_profile_id: uuid.UUID,
) -> OpportunityActivationPolicyVersion:
    with session_scope() as session:
        profile = ensure_active_profile(session, channel_profile_id)
        current = session.scalar(
            select(OpportunityActivationPolicyVersion)
            .where(
                OpportunityActivationPolicyVersion.channel_profile_id == profile.id
            )
            .order_by(OpportunityActivationPolicyVersion.version.desc())
            .limit(1)
        )
        if current is None:
            current = OpportunityActivationPolicyVersion(
                channel_profile_id=profile.id,
                version=1,
                enabled=False,
                min_opportunity_score=Decimal("0.65"),
                min_confidence=Decimal("0.60"),
                min_calibrated_score=None,
                min_rights_readiness=Decimal("0"),
                min_lead_minutes=60,
                max_activations_per_day=3,
                cooldown_minutes=30,
                max_planned_backlog=5,
                min_budget_headroom_usd=Decimal("0.05"),
                max_inspected_per_run=20,
                item_count=None,
                policy_metadata={"created_by": "safe_default"},
            )
            session.add(current)
            session.flush()
            session.add(
                DomainEvent(
                    aggregate_type="channel_profile",
                    aggregate_id=str(profile.id),
                    event_type="channel_profile.opportunity_activation_policy_created",
                    payload={
                        "channel_profile_id": str(profile.id),
                        "policy_version": 1,
                        "enabled": False,
                    },
                )
            )
        session.refresh(current)
        session.expunge(current)
        return current


def create_activation_policy_version(
    channel_profile_id: uuid.UUID,
    *,
    enabled: bool,
    min_opportunity_score: float,
    min_confidence: float,
    min_calibrated_score: float | None,
    min_rights_readiness: float,
    min_lead_minutes: int,
    max_activations_per_day: int,
    cooldown_minutes: int,
    max_planned_backlog: int,
    min_budget_headroom_usd: Decimal | float | str,
    max_inspected_per_run: int,
    item_count: int | None,
    metadata: dict[str, object] | None = None,
    actor: str = "operator",
) -> OpportunityActivationPolicyVersion:
    values = {
        "min_opportunity_score": min_opportunity_score,
        "min_confidence": min_confidence,
        "min_rights_readiness": min_rights_readiness,
    }
    if min_calibrated_score is not None:
        values["min_calibrated_score"] = min_calibrated_score
    if any(float(value) < 0 or float(value) > 1 for value in values.values()):
        raise ValueError("activation score/confidence thresholds must be between 0 and 1")
    if min_lead_minutes < 0 or max_activations_per_day < 0 or cooldown_minutes < 0:
        raise ValueError("activation time and daily limits must be nonnegative")
    if max_planned_backlog < 0:
        raise ValueError("max_planned_backlog must be nonnegative")
    if max_inspected_per_run < 1:
        raise ValueError("max_inspected_per_run must be positive")
    if item_count is not None and item_count < 1:
        raise ValueError("item_count must be positive")
    budget_floor = Decimal(str(min_budget_headroom_usd))
    if budget_floor < 0:
        raise ValueError("min_budget_headroom_usd must be nonnegative")

    with session_scope() as session:
        profile = ensure_active_profile(session, channel_profile_id)
        current_version = session.scalar(
            select(func.coalesce(func.max(OpportunityActivationPolicyVersion.version), 0))
            .where(OpportunityActivationPolicyVersion.channel_profile_id == profile.id)
        )
        version = int(current_version or 0) + 1
        row = OpportunityActivationPolicyVersion(
            channel_profile_id=profile.id,
            version=version,
            enabled=enabled,
            min_opportunity_score=Decimal(str(min_opportunity_score)),
            min_confidence=Decimal(str(min_confidence)),
            min_calibrated_score=(
                Decimal(str(min_calibrated_score))
                if min_calibrated_score is not None
                else None
            ),
            min_rights_readiness=Decimal(str(min_rights_readiness)),
            min_lead_minutes=min_lead_minutes,
            max_activations_per_day=max_activations_per_day,
            cooldown_minutes=cooldown_minutes,
            max_planned_backlog=max_planned_backlog,
            min_budget_headroom_usd=budget_floor,
            max_inspected_per_run=max_inspected_per_run,
            item_count=item_count,
            policy_metadata={**dict(metadata or {}), "actor": actor},
        )
        session.add(row)
        session.flush()
        session.add(
            DomainEvent(
                aggregate_type="channel_profile",
                aggregate_id=str(profile.id),
                event_type="channel_profile.opportunity_activation_policy_updated",
                payload={
                    "channel_profile_id": str(profile.id),
                    "policy_version": version,
                    "enabled": enabled,
                    "actor": actor,
                },
            )
        )
        session.refresh(row)
        session.expunge(row)
        return row


def _channel_day_start(profile: ChannelProfile, now: datetime) -> datetime:
    try:
        zone = ZoneInfo(profile.timezone or "UTC")
    except Exception:
        zone = ZoneInfo("UTC")
    local = now.astimezone(zone)
    local_start = local.replace(hour=0, minute=0, second=0, microsecond=0)
    return local_start.astimezone(UTC)


def _pipeline_state(
    channel_profile_id: uuid.UUID,
    *,
    now: datetime,
) -> tuple[int, int, datetime | None, Decimal]:
    with session_scope() as session:
        profile = ensure_active_profile(session, channel_profile_id)
        day_start = _channel_day_start(profile, now)
        activated_today = int(
            session.scalar(
                select(func.count(ShortEpisode.id)).where(
                    ShortEpisode.channel_profile_id == profile.id,
                    ShortEpisode.trend_opportunity_id.is_not(None),
                    ShortEpisode.parent_episode_id.is_(None),
                    ShortEpisode.created_at >= day_start,
                )
            )
            or 0
        )
        backlog = int(
            session.scalar(
                select(func.count(ShortEpisode.id)).where(
                    ShortEpisode.channel_profile_id == profile.id,
                    ShortEpisode.parent_episode_id.is_(None),
                    ShortEpisode.status.not_in(_TERMINAL_EPISODE_STATUSES),
                )
            )
            or 0
        )
        last_activation = session.scalar(
            select(func.max(ShortEpisode.created_at)).where(
                ShortEpisode.channel_profile_id == profile.id,
                ShortEpisode.trend_opportunity_id.is_not(None),
                ShortEpisode.parent_episode_id.is_(None),
            )
        )
        economics = budget_state(session, profile, now=now)
        headroom = Decimal(str(economics["budget_headroom_usd"]))
        return activated_today, backlog, last_activation, headroom


def _component(opportunity: TrendOpportunity, key: str) -> float:
    try:
        return float((opportunity.components or {}).get(key) or 0)
    except (TypeError, ValueError):
        return 0.0


def _threshold_reason(
    opportunity: TrendOpportunity,
    policy: OpportunityActivationPolicyVersion,
    *,
    now: datetime,
) -> str | None:
    if float(opportunity.opportunity_score) < float(policy.min_opportunity_score):
        return "opportunity_score_below_threshold"
    if float(opportunity.confidence) < float(policy.min_confidence):
        return "confidence_below_threshold"
    if policy.min_calibrated_score is not None:
        if opportunity.calibrated_score is None:
            return "calibrated_score_unavailable"
        if float(opportunity.calibrated_score) < float(policy.min_calibrated_score):
            return "calibrated_score_below_threshold"
    if _component(opportunity, "rights_readiness") < float(policy.min_rights_readiness):
        return "rights_readiness_below_threshold"
    lead_minutes = (_utc(opportunity.expires_at) - now).total_seconds() / 60
    if lead_minutes < policy.min_lead_minutes:
        return "insufficient_expiry_lead_time"
    return None


def _decision(
    run_id: uuid.UUID,
    channel_profile_id: uuid.UUID,
    opportunity: TrendOpportunity,
    *,
    outcome: str,
    reason: str,
    short_episode_id: uuid.UUID | None = None,
    activation_key: str | None = None,
    metadata: dict[str, object] | None = None,
) -> OpportunityActivationDecision:
    with session_scope() as session:
        existing = session.scalar(
            select(OpportunityActivationDecision).where(
                OpportunityActivationDecision.activation_run_id == run_id,
                OpportunityActivationDecision.trend_opportunity_id == opportunity.id,
            )
        )
        if existing is not None:
            session.expunge(existing)
            return existing
        row = OpportunityActivationDecision(
            activation_run_id=run_id,
            channel_profile_id=channel_profile_id,
            trend_opportunity_id=opportunity.id,
            short_episode_id=short_episode_id,
            outcome=outcome,
            reason=reason,
            activation_key=activation_key,
            decision_metadata=dict(metadata or {}),
        )
        session.add(row)
        session.flush()
        session.refresh(row)
        session.expunge(row)
        return row


def _run_result(run: OpportunityActivationRun) -> ActivationRunResult:
    return ActivationRunResult(
        run_id=run.id,
        channel_profile_id=run.channel_profile_id,
        run_key=run.run_key,
        policy_version=run.policy_version,
        status=run.status,
        scanned_count=run.scanned_count,
        activated_count=run.activated_count,
        skipped_count=run.skipped_count,
    )


def run_opportunity_activation(
    channel_profile_id: uuid.UUID,
    *,
    run_key: str,
    now: datetime | None = None,
) -> ActivationRunResult:
    key = run_key.strip()
    if not key or len(key) > 160:
        raise ValueError("run_key is required and must be 160 characters or fewer")
    current = _utc(now)
    policy = ensure_activation_policy(channel_profile_id)

    with session_scope() as session:
        profile = ensure_active_profile(session, channel_profile_id)
        existing = session.scalar(
            select(OpportunityActivationRun).where(
                OpportunityActivationRun.channel_profile_id == profile.id,
                OpportunityActivationRun.run_key == key,
            )
        )
        if existing is not None and existing.status == "completed":
            session.expunge(existing)
            return _run_result(existing)
        if existing is None:
            run = OpportunityActivationRun(
                channel_profile_id=profile.id,
                run_key=key,
                policy_version=policy.version,
                status="running",
                run_metadata={"started_with_policy_enabled": policy.enabled},
            )
            session.add(run)
            session.flush()
        else:
            run = existing
        run_id = run.id

    if not policy.enabled:
        with session_scope() as session:
            run = session.get(OpportunityActivationRun, run_id)
            assert run is not None
            run.status = "completed"
            run.run_metadata = {**dict(run.run_metadata or {}), "reason": "policy_disabled"}
            run.completed_at = current
            session.refresh(run)
            session.expunge(run)
            return _run_result(run)

    activated_today, backlog, last_activation, headroom = _pipeline_state(
        channel_profile_id,
        now=current,
    )
    with session_scope() as session:
        opportunities = list(
            session.scalars(
                select(TrendOpportunity)
                .where(
                    TrendOpportunity.channel_profile_id == channel_profile_id,
                    TrendOpportunity.expires_at > current,
                )
                .order_by(
                    func.coalesce(
                        TrendOpportunity.calibrated_score,
                        TrendOpportunity.opportunity_score,
                    ).desc(),
                    TrendOpportunity.confidence.desc(),
                    TrendOpportunity.created_at.desc(),
                )
                .limit(policy.max_inspected_per_run)
            )
        )
        for opportunity in opportunities:
            session.expunge(opportunity)

    activated = 0
    skipped = 0
    for opportunity in opportunities:
        with session_scope() as session:
            existing_decision = session.scalar(
                select(OpportunityActivationDecision).where(
                    OpportunityActivationDecision.activation_run_id == run_id,
                    OpportunityActivationDecision.trend_opportunity_id == opportunity.id,
                )
            )
        if existing_decision is not None:
            continue

        reason = _threshold_reason(opportunity, policy, now=current)
        if reason is None and activated_today >= policy.max_activations_per_day:
            reason = "daily_activation_limit"
        if reason is None and backlog >= policy.max_planned_backlog:
            reason = "planned_backlog_limit"
        if reason is None and headroom < policy.min_budget_headroom_usd:
            reason = "budget_headroom_below_threshold"
        if reason is None and last_activation is not None:
            eligible_at = _utc(last_activation) + timedelta(minutes=policy.cooldown_minutes)
            if eligible_at > current:
                reason = "activation_cooldown"

        with session_scope() as session:
            episode = session.scalar(
                select(ShortEpisode)
                .where(
                    ShortEpisode.channel_profile_id == channel_profile_id,
                    ShortEpisode.trend_opportunity_id == opportunity.id,
                    ShortEpisode.parent_episode_id.is_(None),
                )
                .order_by(ShortEpisode.created_at.asc())
                .limit(1)
            )
            if episode is not None:
                episode_id = episode.id
            else:
                episode_id = None
        if episode_id is not None:
            _decision(
                run_id,
                channel_profile_id,
                opportunity,
                outcome="skipped",
                reason="already_activated",
                short_episode_id=episode_id,
            )
            skipped += 1
            continue

        if reason is not None:
            _decision(
                run_id,
                channel_profile_id,
                opportunity,
                outcome="skipped",
                reason=reason,
                metadata={
                    "opportunity_score": float(opportunity.opportunity_score),
                    "confidence": float(opportunity.confidence),
                    "calibrated_score": (
                        float(opportunity.calibrated_score)
                        if opportunity.calibrated_score is not None
                        else None
                    ),
                    "rights_readiness": _component(opportunity, "rights_readiness"),
                    "budget_headroom_usd": str(headroom),
                    "backlog": backlog,
                    "activated_today": activated_today,
                },
            )
            skipped += 1
            continue

        try:
            preview = preview_trend_activation(
                channel_profile_id,
                opportunity.id,
                item_count=policy.item_count,
            )
        except (KeyError, ValueError) as exc:
            _decision(
                run_id,
                channel_profile_id,
                opportunity,
                outcome="skipped",
                reason="activation_preview_rejected",
                metadata={"error": str(exc)[:500]},
            )
            skipped += 1
            continue
        if not preview.ready:
            _decision(
                run_id,
                channel_profile_id,
                opportunity,
                outcome="skipped",
                reason=preview.readiness_reason,
                activation_key=preview.activation_key,
                metadata={
                    "eligible_candidate_count": len(preview.eligible),
                    "excluded_candidate_count": len(preview.excluded),
                    "required_item_count": preview.item_count,
                },
            )
            skipped += 1
            continue

        episode, preview = activate_trend_opportunity(
            channel_profile_id,
            opportunity.id,
            item_count=policy.item_count,
            actor="system",
        )
        _decision(
            run_id,
            channel_profile_id,
            opportunity,
            outcome="activated",
            reason="policy_qualified",
            short_episode_id=episode.id,
            activation_key=preview.activation_key,
            metadata={
                "eligible_candidate_count": len(preview.eligible),
                "excluded_candidate_count": len(preview.excluded),
                "item_count": preview.item_count,
            },
        )
        activated += 1
        activated_today += 1
        backlog += 1
        last_activation = current

    with session_scope() as session:
        run = session.get(OpportunityActivationRun, run_id)
        assert run is not None
        decisions = list(
            session.scalars(
                select(OpportunityActivationDecision).where(
                    OpportunityActivationDecision.activation_run_id == run.id
                )
            )
        )
        run.scanned_count = len(decisions)
        run.activated_count = sum(1 for item in decisions if item.outcome == "activated")
        run.skipped_count = len(decisions) - run.activated_count
        run.status = "completed"
        run.completed_at = current
        run.run_metadata = {
            **dict(run.run_metadata or {}),
            "budget_headroom_usd": str(headroom),
            "starting_backlog": max(backlog - activated, 0),
            "ending_backlog": backlog,
            "starting_activated_today": max(activated_today - activated, 0),
            "ending_activated_today": activated_today,
        }
        session.add(
            DomainEvent(
                aggregate_type="channel_profile",
                aggregate_id=str(channel_profile_id),
                event_type="channel_profile.opportunity_activation_completed",
                payload={
                    "channel_profile_id": str(channel_profile_id),
                    "activation_run_id": str(run.id),
                    "run_key": key,
                    "policy_version": policy.version,
                    "scanned_count": run.scanned_count,
                    "activated_count": run.activated_count,
                    "skipped_count": run.skipped_count,
                },
            )
        )
        session.flush()
        session.refresh(run)
        session.expunge(run)
        return _run_result(run)


def list_activation_decisions(
    run_id: uuid.UUID,
) -> list[OpportunityActivationDecision]:
    with session_scope() as session:
        rows = list(
            session.scalars(
                select(OpportunityActivationDecision)
                .where(OpportunityActivationDecision.activation_run_id == run_id)
                .order_by(OpportunityActivationDecision.created_at, OpportunityActivationDecision.id)
            )
        )
        for row in rows:
            session.expunge(row)
        return rows
