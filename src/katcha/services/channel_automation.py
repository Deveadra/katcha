from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import func, select

from katcha.db import session_scope
from katcha.domain import AutomationLevel, PublicationStatus, ReviewDecision
from katcha.intelligence_models import AutomationPolicyVersion, RankingSnapshot
from katcha.longform_models import Compilation, CompilationReview
from katcha.models import DomainEvent
from katcha.production_models import Production, ProductionReview
from katcha.publishing_models import Publication
from katcha.services.channel_profiles import active_automation, ensure_active_profile

_LEVELS = (
    AutomationLevel.REVIEW_REQUIRED,
    AutomationLevel.AUTO_APPROVE_LOW_RISK,
    AutomationLevel.AUTO_PUBLISH_PRIVATE,
    AutomationLevel.AUTO_PUBLISH_SCHEDULED,
)
_DEFAULT_RECENT_REVIEW_WINDOW = 20
_DEFAULT_MAX_RECENT_REVIEW_DRIFT = 0.15
_MIN_RECENT_DRIFT_SAMPLE = 10


@dataclass(frozen=True, slots=True)
class AutomationEvidence:
    reviewed_items: int
    approval_rate: float
    rejection_rate: float
    regeneration_rate: float
    publication_failure_rate: float
    ranking_confidence: float
    recent_negative_review_rate: float
    review_drift_delta: float
    recent_review_count: int
    recent_review_window: int
    max_recent_review_drift: float
    eligible: bool
    reasons: tuple[str, ...]


def _ratio(numerator: int, denominator: int) -> float:
    return numerator / denominator if denominator > 0 else 0.0


def _review_rows(channel_profile_id: uuid.UUID) -> list[tuple[str, object]]:
    with session_scope() as session:
        production_rows = list(
            session.execute(
                select(ProductionReview.decision, ProductionReview.created_at)
                .join(Production, ProductionReview.production_id == Production.id)
                .where(Production.channel_profile_id == channel_profile_id)
            )
        )
        compilation_rows = list(
            session.execute(
                select(CompilationReview.decision, CompilationReview.created_at)
                .join(
                    Compilation,
                    CompilationReview.compilation_id == Compilation.id,
                )
                .where(Compilation.channel_profile_id == channel_profile_id)
            )
        )
    rows = [(str(decision), created_at) for decision, created_at in [*production_rows, *compilation_rows]]
    return sorted(rows, key=lambda item: item[1])


def _review_statistics(
    channel_profile_id: uuid.UUID,
    *,
    recent_window: int,
) -> tuple[int, int, int, int, int, float, float]:
    rows = _review_rows(channel_profile_id)
    reviewed = len(rows)
    approved = sum(decision == ReviewDecision.APPROVE.value for decision, _ in rows)
    rejected = sum(decision == ReviewDecision.REJECT.value for decision, _ in rows)
    regenerated = sum(decision == ReviewDecision.REGENERATE.value for decision, _ in rows)
    recent = rows[-recent_window:] if recent_window > 0 else []
    recent_negative = sum(
        decision in {ReviewDecision.REJECT.value, ReviewDecision.REGENERATE.value}
        for decision, _ in recent
    )
    recent_negative_rate = _ratio(recent_negative, len(recent))
    overall_negative_rate = _ratio(rejected + regenerated, reviewed)
    drift_delta = recent_negative_rate - overall_negative_rate
    return (
        reviewed,
        approved,
        rejected,
        regenerated,
        len(recent),
        recent_negative_rate,
        drift_delta,
    )


def _drift_policy(policy: AutomationPolicyVersion) -> tuple[int, float]:
    metadata = dict(policy.policy_metadata or {})
    try:
        window = int(metadata.get("recent_review_window", _DEFAULT_RECENT_REVIEW_WINDOW))
    except (TypeError, ValueError):
        window = _DEFAULT_RECENT_REVIEW_WINDOW
    try:
        max_drift = float(
            metadata.get("max_recent_review_drift", _DEFAULT_MAX_RECENT_REVIEW_DRIFT)
        )
    except (TypeError, ValueError):
        max_drift = _DEFAULT_MAX_RECENT_REVIEW_DRIFT
    return max(10, min(window, 200)), max(0.0, min(max_drift, 1.0))


def evaluate_automation(channel_profile_id: uuid.UUID) -> AutomationEvidence:
    with session_scope() as session:
        profile = ensure_active_profile(session, channel_profile_id)
        policy = active_automation(session, profile)
        recent_window, max_drift = _drift_policy(policy)

    (
        reviewed,
        approved,
        rejected,
        regenerated,
        recent_count,
        recent_negative_rate,
        drift_delta,
    ) = _review_statistics(channel_profile_id, recent_window=recent_window)

    with session_scope() as session:
        profile = ensure_active_profile(session, channel_profile_id)
        policy = active_automation(session, profile)
        publication_total = int(
            session.scalar(
                select(func.count(Publication.id)).where(
                    Publication.youtube_connection_id == profile.youtube_connection_id
                )
            )
            or 0
        )
        publication_failed = int(
            session.scalar(
                select(func.count(Publication.id)).where(
                    Publication.youtube_connection_id == profile.youtube_connection_id,
                    Publication.status == PublicationStatus.FAILED.value,
                )
            )
            or 0
        )
        ranking = session.scalar(
            select(RankingSnapshot)
            .where(RankingSnapshot.channel_profile_id == profile.id)
            .order_by(RankingSnapshot.version.desc())
            .limit(1)
        )

        approval_rate = _ratio(approved, reviewed)
        rejection_rate = _ratio(rejected, reviewed)
        regeneration_rate = _ratio(regenerated, reviewed)
        failure_rate = _ratio(publication_failed, publication_total)
        ranking_confidence = float(ranking.confidence) if ranking else 0.0
        reasons: list[str] = []
        if reviewed < policy.min_reviewed_items:
            reasons.append("insufficient_reviewed_items")
        if approval_rate < float(policy.min_approval_rate):
            reasons.append("approval_rate_below_threshold")
        if regeneration_rate > float(policy.max_regeneration_rate):
            reasons.append("regeneration_rate_above_threshold")
        if failure_rate > float(policy.max_publication_failure_rate):
            reasons.append("publication_failure_rate_above_threshold")
        if ranking_confidence < float(policy.min_ranking_confidence):
            reasons.append("ranking_confidence_below_threshold")
        if (
            recent_count >= _MIN_RECENT_DRIFT_SAMPLE
            and drift_delta > max_drift
        ):
            reasons.append("recent_review_drift_above_threshold")
        return AutomationEvidence(
            reviewed_items=reviewed,
            approval_rate=round(approval_rate, 6),
            rejection_rate=round(rejection_rate, 6),
            regeneration_rate=round(regeneration_rate, 6),
            publication_failure_rate=round(failure_rate, 6),
            ranking_confidence=round(ranking_confidence, 6),
            recent_negative_review_rate=round(recent_negative_rate, 6),
            review_drift_delta=round(drift_delta, 6),
            recent_review_count=recent_count,
            recent_review_window=recent_window,
            max_recent_review_drift=round(max_drift, 6),
            eligible=not reasons,
            reasons=tuple(reasons),
        )


def _next_level(level: AutomationLevel) -> AutomationLevel | None:
    index = _LEVELS.index(level)
    return _LEVELS[index + 1] if index + 1 < len(_LEVELS) else None


def _evidence_payload(evidence: AutomationEvidence) -> dict[str, object]:
    return {
        "reviewed_items": evidence.reviewed_items,
        "approval_rate": evidence.approval_rate,
        "rejection_rate": evidence.rejection_rate,
        "regeneration_rate": evidence.regeneration_rate,
        "publication_failure_rate": evidence.publication_failure_rate,
        "ranking_confidence": evidence.ranking_confidence,
        "recent_negative_review_rate": evidence.recent_negative_review_rate,
        "review_drift_delta": evidence.review_drift_delta,
        "recent_review_count": evidence.recent_review_count,
        "recent_review_window": evidence.recent_review_window,
        "max_recent_review_drift": evidence.max_recent_review_drift,
        "eligible": evidence.eligible,
        "reasons": list(evidence.reasons),
    }


def _new_policy_version(
    channel_profile_id: uuid.UUID,
    *,
    level: AutomationLevel,
    actor: str,
    evidence: AutomationEvidence,
    action: str,
) -> AutomationPolicyVersion:
    with session_scope() as session:
        profile = ensure_active_profile(session, channel_profile_id)
        current = active_automation(session, profile)
        version = profile.active_automation_version + 1
        metadata = dict(current.policy_metadata or {})
        metadata.update(
            {
                "actor": actor,
                "action": action,
                "supersedes": current.version,
                "recent_review_window": evidence.recent_review_window,
                "max_recent_review_drift": evidence.max_recent_review_drift,
                "evidence": _evidence_payload(evidence),
            }
        )
        row = AutomationPolicyVersion(
            channel_profile_id=profile.id,
            version=version,
            level=level.value,
            min_reviewed_items=current.min_reviewed_items,
            min_approval_rate=current.min_approval_rate,
            max_regeneration_rate=current.max_regeneration_rate,
            max_publication_failure_rate=current.max_publication_failure_rate,
            min_ranking_confidence=current.min_ranking_confidence,
            auto_demote=current.auto_demote,
            policy_metadata=metadata,
        )
        session.add(row)
        profile.active_automation_version = version
        session.add(
            DomainEvent(
                aggregate_type="channel_profile",
                aggregate_id=str(profile.id),
                event_type=f"channel_profile.automation_{action}",
                payload={
                    "channel_profile_id": str(profile.id),
                    "automation_version": version,
                    "previous_level": current.level,
                    "level": level.value,
                    "actor": actor,
                    "evidence": row.policy_metadata["evidence"],
                },
            )
        )
        session.flush()
        session.refresh(row)
        session.expunge(row)
        return row


def promote_automation(
    channel_profile_id: uuid.UUID,
    *,
    target_level: AutomationLevel,
    actor: str = "operator",
) -> AutomationPolicyVersion:
    evidence = evaluate_automation(channel_profile_id)
    if not evidence.eligible:
        raise ValueError(
            "automation promotion gates are not satisfied: "
            + ", ".join(evidence.reasons)
        )
    with session_scope() as session:
        profile = ensure_active_profile(session, channel_profile_id)
        current = active_automation(session, profile)
        expected = _next_level(AutomationLevel(current.level))
    if expected is None:
        raise ValueError("channel is already at the highest automation level")
    if target_level != expected:
        raise ValueError(
            f"automation can only advance one level at a time; next is {expected.value}"
        )
    return _new_policy_version(
        channel_profile_id,
        level=target_level,
        actor=actor,
        evidence=evidence,
        action="promoted",
    )


def maybe_auto_demote(
    channel_profile_id: uuid.UUID,
) -> AutomationPolicyVersion | None:
    evidence = evaluate_automation(channel_profile_id)
    with session_scope() as session:
        profile = ensure_active_profile(session, channel_profile_id)
        current = active_automation(session, profile)
        current_level = AutomationLevel(current.level)
        should_demote = (
            current.auto_demote
            and current_level != AutomationLevel.REVIEW_REQUIRED
            and not evidence.eligible
        )
    if not should_demote:
        return None
    return _new_policy_version(
        channel_profile_id,
        level=AutomationLevel.REVIEW_REQUIRED,
        actor="system",
        evidence=evidence,
        action="demoted",
    )


def automation_summary(channel_profile_id: uuid.UUID) -> dict[str, object]:
    evidence = evaluate_automation(channel_profile_id)
    with session_scope() as session:
        profile = ensure_active_profile(session, channel_profile_id)
        policy = active_automation(session, profile)
        next_level = _next_level(AutomationLevel(policy.level))
        return {
            "level": policy.level,
            "version": policy.version,
            "next_level": next_level.value if next_level else None,
            "eligible_for_next_level": evidence.eligible and next_level is not None,
            "evidence": _evidence_payload(evidence),
        }
