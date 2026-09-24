from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import and_, exists, func, select

from katcha.config import get_settings
from katcha.db import session_scope
from katcha.intelligence_models import ChannelProfile
from katcha.models import DomainEvent
from katcha.publishing_models import Publication
from katcha.services.channel_economics import latest_economics_snapshot
from katcha.services.channel_profiles import ensure_active_profile
from katcha.services.trend_activation import (
    activate_trend_opportunity,
    preview_trend_activation,
)
from katcha.services.trend_source_reliability import (
    channel_trend_source_health,
    source_health_allows_refresh,
)
from katcha.short_episode_models import ShortEpisode
from katcha.trend_activation_models import (
    TrendActivationDecision,
    TrendActivationPolicyVersion,
    TrendActivationRun,
)
from katcha.trend_models import TrendOpportunity

_TERMINAL_RUN_STATUSES = {"completed", "disabled"}
_DECISION_ACTIVATED = "activated"
_DECISION_ALREADY = "already_activated"
_DECISION_DEFERRED = {
    "budget",
    "backlog",
    "cooldown",
    "daily_cap",
    "source_health",
}
_DECISION_SKIPPED = {
    "expired",
    "threshold",
    "confidence",
    "calibrated_threshold",
    "calibrated_unavailable",
    "rights_readiness",
    "lead_time",
    "not_ready",
}


@dataclass(frozen=True, slots=True)
class ActivationRunSummary:
    run_id: uuid.UUID
    channel_profile_id: uuid.UUID
    run_key: str
    policy_version: int | None
    status: str
    inspected_count: int
    activated_count: int
    deferred_count: int
    skipped_count: int
    reason: str | None

    def as_dict(self) -> dict[str, object]:
        return {
            "run_id": str(self.run_id),
            "channel_profile_id": str(self.channel_profile_id),
            "run_key": self.run_key,
            "policy_version": self.policy_version,
            "status": self.status,
            "inspected_count": self.inspected_count,
            "activated_count": self.activated_count,
            "deferred_count": self.deferred_count,
            "skipped_count": self.skipped_count,
            "reason": self.reason,
        }


def _utc(value: datetime | None = None) -> datetime:
    current = value or datetime.now(UTC)
    if current.tzinfo is None:
        return current.replace(tzinfo=UTC)
    return current.astimezone(UTC)


def _policy_snapshot(policy: TrendActivationPolicyVersion) -> dict[str, object]:
    return {
        "version": policy.version,
        "enabled": policy.enabled,
        "min_opportunity_score": float(policy.min_opportunity_score),
        "min_confidence": float(policy.min_confidence),
        "min_calibrated_score": (
            float(policy.min_calibrated_score)
            if policy.min_calibrated_score is not None
            else None
        ),
        "min_rights_readiness": float(policy.min_rights_readiness),
        "min_lead_time_minutes": policy.min_lead_time_minutes,
        "max_activations_per_day": policy.max_activations_per_day,
        "cooldown_minutes": policy.cooldown_minutes,
        "max_backlog": policy.max_backlog,
        "min_budget_headroom_usd": str(policy.min_budget_headroom_usd),
        "max_opportunities_per_run": policy.max_opportunities_per_run,
    }


def active_activation_policy(
    channel_profile_id: uuid.UUID,
) -> TrendActivationPolicyVersion | None:
    with session_scope() as session:
        return session.scalar(
            select(TrendActivationPolicyVersion)
            .where(TrendActivationPolicyVersion.channel_profile_id == channel_profile_id)
            .order_by(TrendActivationPolicyVersion.version.desc())
            .limit(1)
        )


def create_activation_policy(
    channel_profile_id: uuid.UUID,
    *,
    enabled: bool,
    min_opportunity_score: float = 0.65,
    min_confidence: float = 0.55,
    min_calibrated_score: float | None = None,
    min_rights_readiness: float = 0.50,
    min_lead_time_minutes: int = 120,
    max_activations_per_day: int = 3,
    cooldown_minutes: int = 120,
    max_backlog: int = 5,
    min_budget_headroom_usd: Decimal | float | str = Decimal("2.00"),
    max_opportunities_per_run: int = 25,
    metadata: dict[str, Any] | None = None,
    actor: str = "operator",
) -> TrendActivationPolicyVersion:
    probability_values = {
        "min_opportunity_score": min_opportunity_score,
        "min_confidence": min_confidence,
        "min_rights_readiness": min_rights_readiness,
    }
    if min_calibrated_score is not None:
        probability_values["min_calibrated_score"] = min_calibrated_score
    for name, value in probability_values.items():
        if not 0 <= float(value) <= 1:
            raise ValueError(f"{name} must be between 0 and 1")
    integer_values = {
        "min_lead_time_minutes": min_lead_time_minutes,
        "max_activations_per_day": max_activations_per_day,
        "cooldown_minutes": cooldown_minutes,
        "max_backlog": max_backlog,
    }
    for name, value in integer_values.items():
        if int(value) < 0:
            raise ValueError(f"{name} must be non-negative")
    if max_opportunities_per_run < 1:
        raise ValueError("max_opportunities_per_run must be positive")
    budget = Decimal(str(min_budget_headroom_usd))
    if budget < 0:
        raise ValueError("min_budget_headroom_usd must be non-negative")

    with session_scope() as session:
        if session.get(ChannelProfile, channel_profile_id) is None:
            raise ValueError(f"channel profile not found: {channel_profile_id}")
        current = session.scalar(
            select(func.max(TrendActivationPolicyVersion.version)).where(
                TrendActivationPolicyVersion.channel_profile_id == channel_profile_id
            )
        )
        policy = TrendActivationPolicyVersion(
            channel_profile_id=channel_profile_id,
            version=int(current or 0) + 1,
            enabled=enabled,
            min_opportunity_score=Decimal(str(min_opportunity_score)),
            min_confidence=Decimal(str(min_confidence)),
            min_calibrated_score=(
                Decimal(str(min_calibrated_score))
                if min_calibrated_score is not None
                else None
            ),
            min_rights_readiness=Decimal(str(min_rights_readiness)),
            min_lead_time_minutes=min_lead_time_minutes,
            max_activations_per_day=max_activations_per_day,
            cooldown_minutes=cooldown_minutes,
            max_backlog=max_backlog,
            min_budget_headroom_usd=budget,
            max_opportunities_per_run=max_opportunities_per_run,
            policy_metadata=metadata or {},
        )
        session.add(policy)
        session.flush()
        session.add(
            DomainEvent(
                aggregate_type="trend_activation_policy",
                aggregate_id=str(channel_profile_id),
                event_type="trend.activation.policy_versioned",
                payload={
                    "channel_profile_id": str(channel_profile_id),
                    "version": policy.version,
                    "enabled": policy.enabled,
                    "actor": actor,
                },
            )
        )
        return policy


def _day_start_utc(profile: ChannelProfile, now: datetime) -> datetime:
    try:
        timezone = ZoneInfo(profile.timezone)
    except ZoneInfoNotFoundError:
        timezone = UTC
    local = now.astimezone(timezone)
    local_start = local.replace(hour=0, minute=0, second=0, microsecond=0)
    return local_start.astimezone(UTC)


def _activation_roots(
    session: object,
    channel_profile_id: uuid.UUID,
    *,
    since: datetime | None = None,
) -> list[ShortEpisode]:
    stmt = select(ShortEpisode).where(
        ShortEpisode.channel_profile_id == channel_profile_id,
        ShortEpisode.trend_opportunity_id.is_not(None),
        ShortEpisode.parent_episode_id.is_(None),
        ShortEpisode.generation == 1,
    )
    if since is not None:
        stmt = stmt.where(ShortEpisode.created_at >= since)
    return list(session.scalars(stmt.order_by(ShortEpisode.created_at.desc())))


def _backlog_count(session: object, channel_profile_id: uuid.UUID) -> int:
    value = session.scalar(
        select(func.count(ShortEpisode.id)).where(
            ShortEpisode.channel_profile_id == channel_profile_id,
            ShortEpisode.status.not_in(("rejected", "failed")),
            ~exists(
                select(Publication.id).where(
                    Publication.short_episode_id == ShortEpisode.id
                )
            ),
        )
    )
    return int(value or 0)


def _episode_for_activation_key(
    session: object,
    channel_profile_id: uuid.UUID,
    opportunity_id: uuid.UUID,
    activation_key: str,
) -> ShortEpisode | None:
    episodes = list(
        session.scalars(
            select(ShortEpisode)
            .where(
                ShortEpisode.channel_profile_id == channel_profile_id,
                ShortEpisode.trend_opportunity_id == opportunity_id,
                ShortEpisode.parent_episode_id.is_(None),
            )
            .order_by(ShortEpisode.created_at)
        )
    )
    for episode in episodes:
        planning = dict((episode.plan_snapshot or {}).get("planning_metadata") or {})
        trend_activation = dict(planning.get("trend_activation") or {})
        if str(trend_activation.get("activation_key") or "") == activation_key:
            return episode
    return None


def _latest_opportunities(
    channel_profile_id: uuid.UUID,
    *,
    now: datetime,
    limit: int,
) -> list[TrendOpportunity]:
    with session_scope() as session:
        latest = (
            select(
                TrendOpportunity.trend_topic_id.label("trend_topic_id"),
                func.max(TrendOpportunity.created_at).label("created_at"),
            )
            .where(TrendOpportunity.channel_profile_id == channel_profile_id)
            .group_by(TrendOpportunity.trend_topic_id)
            .subquery()
        )
        return list(
            session.scalars(
                select(TrendOpportunity)
                .join(
                    latest,
                    and_(
                        TrendOpportunity.trend_topic_id == latest.c.trend_topic_id,
                        TrendOpportunity.created_at == latest.c.created_at,
                    ),
                )
                .where(
                    TrendOpportunity.channel_profile_id == channel_profile_id,
                    TrendOpportunity.expires_at > now,
                )
                .order_by(
                    func.coalesce(
                        TrendOpportunity.calibrated_score,
                        TrendOpportunity.opportunity_score,
                    ).desc(),
                    TrendOpportunity.confidence.desc(),
                    TrendOpportunity.created_at.desc(),
                )
                .limit(limit)
            )
        )


def _run_summary(run: TrendActivationRun) -> ActivationRunSummary:
    metadata = dict(run.run_metadata or {})
    return ActivationRunSummary(
        run_id=run.id,
        channel_profile_id=run.channel_profile_id,
        run_key=run.run_key,
        policy_version=run.policy_version,
        status=run.status,
        inspected_count=run.inspected_count,
        activated_count=run.activated_count,
        deferred_count=run.deferred_count,
        skipped_count=run.skipped_count,
        reason=str(metadata.get("reason")) if metadata.get("reason") else None,
    )


def _decision_reason(
    opportunity: TrendOpportunity,
    policy: TrendActivationPolicyVersion,
    *,
    now: datetime,
) -> tuple[str | None, str | None, float, int]:
    rights_readiness = max(
        0.0,
        min(1.0, float((opportunity.components or {}).get("rights_readiness") or 0.0)),
    )
    expires_at = _utc(opportunity.expires_at)
    lead_minutes = max(0, int((expires_at - now).total_seconds() // 60))
    if expires_at <= now:
        return "expired", "opportunity_expired", rights_readiness, lead_minutes
    if float(opportunity.opportunity_score) < float(policy.min_opportunity_score):
        return "threshold", "opportunity_score_below_policy", rights_readiness, lead_minutes
    if float(opportunity.confidence) < float(policy.min_confidence):
        return "confidence", "confidence_below_policy", rights_readiness, lead_minutes
    if policy.min_calibrated_score is not None:
        if opportunity.calibrated_score is None:
            return (
                "calibrated_unavailable",
                "calibrated_score_required_but_unavailable",
                rights_readiness,
                lead_minutes,
            )
        if float(opportunity.calibrated_score) < float(policy.min_calibrated_score):
            return (
                "calibrated_threshold",
                "calibrated_score_below_policy",
                rights_readiness,
                lead_minutes,
            )
    if rights_readiness < float(policy.min_rights_readiness):
        return (
            "rights_readiness",
            "rights_readiness_below_policy",
            rights_readiness,
            lead_minutes,
        )
    if lead_minutes < policy.min_lead_time_minutes:
        return "lead_time", "insufficient_remaining_lead_time", rights_readiness, lead_minutes
    return None, None, rights_readiness, lead_minutes


def _record_decision(
    run_id: uuid.UUID,
    opportunity: TrendOpportunity,
    *,
    decision: str,
    reason: str,
    rights_readiness: float,
    remaining_lead_minutes: int,
    packet_id: uuid.UUID | None = None,
    short_episode_id: uuid.UUID | None = None,
    activation_key: str | None = None,
    snapshot: dict[str, Any] | None = None,
) -> TrendActivationDecision:
    with session_scope() as session:
        existing = session.scalar(
            select(TrendActivationDecision).where(
                TrendActivationDecision.activation_run_id == run_id,
                TrendActivationDecision.trend_opportunity_id == opportunity.id,
            )
        )
        if existing is not None:
            return existing
        row = TrendActivationDecision(
            activation_run_id=run_id,
            channel_profile_id=opportunity.channel_profile_id,
            trend_opportunity_id=opportunity.id,
            trend_evidence_packet_id=packet_id,
            short_episode_id=short_episode_id,
            activation_key=activation_key,
            decision=decision,
            reason=reason,
            opportunity_score=opportunity.opportunity_score,
            confidence=opportunity.confidence,
            calibrated_score=opportunity.calibrated_score,
            rights_readiness=Decimal(str(rights_readiness)),
            remaining_lead_minutes=remaining_lead_minutes,
            decision_snapshot=snapshot or {},
        )
        session.add(row)
        session.add(
            DomainEvent(
                aggregate_type="trend_activation_run",
                aggregate_id=str(run_id),
                event_type="trend.activation.decision",
                payload={
                    "activation_run_id": str(run_id),
                    "channel_profile_id": str(opportunity.channel_profile_id),
                    "trend_opportunity_id": str(opportunity.id),
                    "decision": decision,
                    "reason": reason,
                    "short_episode_id": (
                        str(short_episode_id) if short_episode_id else None
                    ),
                    "activation_key": activation_key,
                },
            )
        )
        session.flush()
        return row


def _complete_run(
    run_id: uuid.UUID,
    *,
    now: datetime,
    reason: str | None = None,
) -> ActivationRunSummary:
    with session_scope() as session:
        run = session.get(TrendActivationRun, run_id)
        if run is None:
            raise RuntimeError("trend activation run disappeared")
        decisions = list(
            session.scalars(
                select(TrendActivationDecision).where(
                    TrendActivationDecision.activation_run_id == run_id
                )
            )
        )
        run.inspected_count = len(decisions)
        run.activated_count = sum(
            1 for item in decisions if item.decision == _DECISION_ACTIVATED
        )
        run.deferred_count = sum(
            1 for item in decisions if item.decision in _DECISION_DEFERRED
        )
        run.skipped_count = sum(
            1
            for item in decisions
            if item.decision in _DECISION_SKIPPED or item.decision == _DECISION_ALREADY
        )
        run.status = "completed"
        run.completed_at = now
        run.run_metadata = {
            **dict(run.run_metadata or {}),
            **({"reason": reason} if reason else {}),
        }
        session.add(
            DomainEvent(
                aggregate_type="trend_activation_run",
                aggregate_id=str(run.id),
                event_type="trend.activation.run_completed",
                payload={
                    "activation_run_id": str(run.id),
                    "channel_profile_id": str(run.channel_profile_id),
                    "run_key": run.run_key,
                    "policy_version": run.policy_version,
                    "inspected_count": run.inspected_count,
                    "activated_count": run.activated_count,
                    "deferred_count": run.deferred_count,
                    "skipped_count": run.skipped_count,
                    "reason": reason,
                },
            )
        )
        return _run_summary(run)


def run_autonomous_trend_activation(
    channel_profile_id: uuid.UUID,
    *,
    run_key: str,
    now: datetime | None = None,
) -> ActivationRunSummary:
    key = run_key.strip()
    if not key:
        raise ValueError("trend activation run_key is required")
    if len(key) > 160:
        raise ValueError("trend activation run_key must be 160 characters or fewer")
    reference = _utc(now)

    with session_scope() as session:
        profile = ensure_active_profile(session, channel_profile_id)
        existing = session.scalar(
            select(TrendActivationRun).where(
                TrendActivationRun.channel_profile_id == profile.id,
                TrendActivationRun.run_key == key,
            )
        )
        if existing is not None and existing.status in _TERMINAL_RUN_STATUSES:
            return _run_summary(existing)
        policy = session.scalar(
            select(TrendActivationPolicyVersion)
            .where(TrendActivationPolicyVersion.channel_profile_id == profile.id)
            .order_by(TrendActivationPolicyVersion.version.desc())
            .limit(1)
        )
        if existing is None:
            existing = TrendActivationRun(
                channel_profile_id=profile.id,
                policy_version=policy.version if policy else None,
                run_key=key,
                status="running",
                run_metadata={},
                started_at=reference,
            )
            session.add(existing)
            session.flush()
        run_id = existing.id
        profile_timezone = profile.timezone

    if policy is None or not policy.enabled:
        with session_scope() as session:
            run = session.get(TrendActivationRun, run_id)
            if run is None:
                raise RuntimeError("trend activation run disappeared")
            run.status = "disabled"
            run.completed_at = reference
            run.run_metadata = {"reason": "activation_policy_disabled_or_missing"}
            return _run_summary(run)

    source_health = channel_trend_source_health(channel_profile_id)
    settings = get_settings()
    source_healthy = source_health_allows_refresh(
        source_health,
        minimum_coverage=settings.trend_min_source_coverage,
    )

    with session_scope() as session:
        profile = ensure_active_profile(session, channel_profile_id)
        economics = latest_economics_snapshot(session, profile)
        day_start = _day_start_utc(profile, reference)
        activations_today = _activation_roots(
            session,
            channel_profile_id,
            since=day_start,
        )
        all_activations = _activation_roots(session, channel_profile_id)
        latest_activation_at = (
            _utc(all_activations[0].created_at) if all_activations else None
        )
        backlog = _backlog_count(session, channel_profile_id)
        economics_id = str(economics.id) if economics else None
        budget_headroom = (
            Decimal(economics.budget_headroom_usd) if economics else None
        )
        policy_snapshot = _policy_snapshot(policy)

    opportunities = _latest_opportunities(
        channel_profile_id,
        now=reference,
        limit=policy.max_opportunities_per_run,
    )

    for opportunity in opportunities:
        with session_scope() as session:
            prior_decision = session.scalar(
                select(TrendActivationDecision).where(
                    TrendActivationDecision.activation_run_id == run_id,
                    TrendActivationDecision.trend_opportunity_id == opportunity.id,
                )
            )
        if prior_decision is not None:
            continue

        decision, reason, rights_readiness, lead_minutes = _decision_reason(
            opportunity,
            policy,
            now=reference,
        )
        common_snapshot: dict[str, Any] = {
            "policy": policy_snapshot,
            "source_health": source_health,
            "economics_snapshot_id": economics_id,
            "budget_headroom_usd": (
                str(budget_headroom) if budget_headroom is not None else None
            ),
            "activations_today": len(activations_today),
            "backlog": backlog,
            "latest_activation_at": (
                latest_activation_at.isoformat() if latest_activation_at else None
            ),
            "channel_timezone": profile_timezone,
        }
        if decision is not None and reason is not None:
            _record_decision(
                run_id,
                opportunity,
                decision=decision,
                reason=reason,
                rights_readiness=rights_readiness,
                remaining_lead_minutes=lead_minutes,
                snapshot=common_snapshot,
            )
            continue

        if not source_healthy:
            _record_decision(
                run_id,
                opportunity,
                decision="source_health",
                reason="source_coverage_below_activation_minimum",
                rights_readiness=rights_readiness,
                remaining_lead_minutes=lead_minutes,
                snapshot=common_snapshot,
            )
            continue
        if budget_headroom is None:
            _record_decision(
                run_id,
                opportunity,
                decision="budget",
                reason="economics_snapshot_unavailable",
                rights_readiness=rights_readiness,
                remaining_lead_minutes=lead_minutes,
                snapshot=common_snapshot,
            )
            continue
        if budget_headroom < policy.min_budget_headroom_usd:
            _record_decision(
                run_id,
                opportunity,
                decision="budget",
                reason="budget_headroom_below_policy",
                rights_readiness=rights_readiness,
                remaining_lead_minutes=lead_minutes,
                snapshot=common_snapshot,
            )
            continue
        if backlog >= policy.max_backlog:
            _record_decision(
                run_id,
                opportunity,
                decision="backlog",
                reason="planned_review_backlog_at_capacity",
                rights_readiness=rights_readiness,
                remaining_lead_minutes=lead_minutes,
                snapshot=common_snapshot,
            )
            continue
        if len(activations_today) >= policy.max_activations_per_day:
            _record_decision(
                run_id,
                opportunity,
                decision="daily_cap",
                reason="daily_activation_cap_reached",
                rights_readiness=rights_readiness,
                remaining_lead_minutes=lead_minutes,
                snapshot=common_snapshot,
            )
            continue
        if latest_activation_at is not None:
            cooldown_until = latest_activation_at + timedelta(
                minutes=policy.cooldown_minutes
            )
            if cooldown_until > reference:
                _record_decision(
                    run_id,
                    opportunity,
                    decision="cooldown",
                    reason="channel_activation_cooldown_active",
                    rights_readiness=rights_readiness,
                    remaining_lead_minutes=lead_minutes,
                    snapshot={
                        **common_snapshot,
                        "cooldown_until": cooldown_until.isoformat(),
                    },
                )
                continue

        try:
            preview = preview_trend_activation(channel_profile_id, opportunity.id)
        except ValueError as exc:
            _record_decision(
                run_id,
                opportunity,
                decision="not_ready",
                reason=str(exc)[:1000],
                rights_readiness=rights_readiness,
                remaining_lead_minutes=lead_minutes,
                snapshot=common_snapshot,
            )
            continue

        preview_snapshot = {
            **common_snapshot,
            "preview": {
                "ready": preview.ready,
                "readiness_reason": preview.readiness_reason,
                "eligible_count": len(preview.eligible),
                "excluded_count": len(preview.excluded),
                "item_count": preview.item_count,
                "format_key": preview.format_key,
                "format_version": preview.format_version,
                "activation_key": preview.activation_key,
            },
        }
        if not preview.ready:
            _record_decision(
                run_id,
                opportunity,
                decision="not_ready",
                reason=preview.readiness_reason,
                rights_readiness=rights_readiness,
                remaining_lead_minutes=lead_minutes,
                packet_id=preview.evidence_packet_id,
                activation_key=preview.activation_key,
                snapshot=preview_snapshot,
            )
            continue

        with session_scope() as session:
            existing_episode = _episode_for_activation_key(
                session,
                channel_profile_id,
                opportunity.id,
                preview.activation_key,
            )
        if existing_episode is not None:
            decision_name = _DECISION_ALREADY
            decision_reason = "activation_identity_already_planned"
            with session_scope() as session:
                run = session.get(TrendActivationRun, run_id)
                started_at = _utc(run.started_at) if run is not None else reference
            if _utc(existing_episode.created_at) >= started_at:
                decision_name = _DECISION_ACTIVATED
                decision_reason = "activation_recovered_after_retry"
            _record_decision(
                run_id,
                opportunity,
                decision=decision_name,
                reason=decision_reason,
                rights_readiness=rights_readiness,
                remaining_lead_minutes=lead_minutes,
                packet_id=preview.evidence_packet_id,
                short_episode_id=existing_episode.id,
                activation_key=preview.activation_key,
                snapshot=preview_snapshot,
            )
            if decision_name == _DECISION_ACTIVATED:
                backlog += 1
                activations_today.append(existing_episode)
                latest_activation_at = _utc(existing_episode.created_at)
            continue

        try:
            episode, activated_preview = activate_trend_opportunity(
                channel_profile_id,
                opportunity.id,
                actor="autonomous_activation",
            )
        except ValueError as exc:
            _record_decision(
                run_id,
                opportunity,
                decision="not_ready",
                reason=str(exc)[:1000],
                rights_readiness=rights_readiness,
                remaining_lead_minutes=lead_minutes,
                packet_id=preview.evidence_packet_id,
                activation_key=preview.activation_key,
                snapshot=preview_snapshot,
            )
            continue

        _record_decision(
            run_id,
            opportunity,
            decision=_DECISION_ACTIVATED,
            reason="policy_gates_passed_plan_created",
            rights_readiness=rights_readiness,
            remaining_lead_minutes=lead_minutes,
            packet_id=activated_preview.evidence_packet_id,
            short_episode_id=episode.id,
            activation_key=activated_preview.activation_key,
            snapshot=preview_snapshot,
        )
        backlog += 1
        activations_today.append(episode)
        latest_activation_at = _utc(episode.created_at)

    return _complete_run(run_id, now=reference)


def list_activation_runs(
    channel_profile_id: uuid.UUID,
    *,
    limit: int = 50,
) -> list[TrendActivationRun]:
    with session_scope() as session:
        return list(
            session.scalars(
                select(TrendActivationRun)
                .where(TrendActivationRun.channel_profile_id == channel_profile_id)
                .order_by(TrendActivationRun.started_at.desc())
                .limit(max(1, min(limit, 250)))
            )
        )


def list_activation_decisions(
    run_id: uuid.UUID,
) -> list[TrendActivationDecision]:
    with session_scope() as session:
        if session.get(TrendActivationRun, run_id) is None:
            raise ValueError(f"trend activation run not found: {run_id}")
        return list(
            session.scalars(
                select(TrendActivationDecision)
                .where(TrendActivationDecision.activation_run_id == run_id)
                .order_by(TrendActivationDecision.created_at)
            )
        )
