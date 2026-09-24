from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select

from katcha.db import session_scope
from katcha.domain import AutomationLevel
from katcha.intelligence_models import ChannelProfile
from katcha.models import DomainEvent
from katcha.packaging_intelligence_models import PackagingIntelligenceSnapshot
from katcha.packaging_models import (
    PackagingExperiment,
    PublicationPackagingActivation,
    PublicationPackagingVariant,
)
from katcha.publishing_models import Publication
from katcha.services.channel_profiles import active_automation, ensure_active_profile
from katcha.services.packaging import register_packaging_activation
from katcha.services.packaging_intelligence import latest_packaging_intelligence

_ACTIVE_STATUSES = {"planned", "activating", "observing", "rollback_queued"}
_ROLLBACK_BLOCKERS = {
    "watch_percentage_degraded",
    "midpoint_retention_degraded",
    "publication_margin_negative",
}
_MIN_MUTATION_COOLDOWN = timedelta(hours=24)


@dataclass(frozen=True, slots=True)
class ExperimentEligibility:
    eligible: bool
    reasons: tuple[str, ...]
    publication_id: uuid.UUID
    baseline_variant_id: uuid.UUID | None
    candidate_variant_id: uuid.UUID | None
    intelligence_snapshot_id: uuid.UUID | None
    automation_version: int | None
    maturity_days: int | None
    recommendation: dict[str, Any] | None


@dataclass(frozen=True, slots=True)
class ExperimentAction:
    experiment: PackagingExperiment | None
    activation: PublicationPackagingActivation | None
    action: str
    reason: str


def _publication_profile(
    session: Any,
    publication: Publication,
) -> ChannelProfile:
    profile = session.scalar(
        select(ChannelProfile).where(
            ChannelProfile.youtube_connection_id == publication.youtube_connection_id
        )
    )
    if profile is None:
        raise ValueError("publication has no channel profile")
    return ensure_active_profile(session, profile.id)


def _active_variant_id(publication: Publication) -> uuid.UUID | None:
    active = dict((publication.treatment_metadata or {}).get("active_packaging") or {})
    raw = active.get("variant_id")
    if not raw:
        return None
    try:
        return uuid.UUID(str(raw))
    except ValueError:
        return None


def _recommendation_for_publication(
    snapshot: PackagingIntelligenceSnapshot,
    publication_id: uuid.UUID,
    *,
    recommendation_type: str | None = None,
    candidate_variant_id: uuid.UUID | None = None,
) -> dict[str, Any] | None:
    matches: list[dict[str, Any]] = []
    for raw in snapshot.recommendations or []:
        row = dict(raw)
        if str(row.get("publication_id")) != str(publication_id):
            continue
        if recommendation_type and row.get("recommendation_type") != recommendation_type:
            continue
        if (
            candidate_variant_id is not None
            and str(row.get("candidate_variant_id")) != str(candidate_variant_id)
        ):
            continue
        matches.append(row)
    if not matches:
        return None
    matches.sort(
        key=lambda row: (
            str(row.get("candidate_variant_id") or ""),
            str(row.get("baseline_variant_id") or ""),
        )
    )
    return matches[0]


def _latest_mutation(
    session: Any,
    publication_id: uuid.UUID,
) -> PublicationPackagingActivation | None:
    return session.scalar(
        select(PublicationPackagingActivation)
        .where(PublicationPackagingActivation.publication_id == publication_id)
        .order_by(
            PublicationPackagingActivation.applied_at.desc().nullslast(),
            PublicationPackagingActivation.created_at.desc(),
        )
        .limit(1)
    )


def experiment_eligibility(publication_id: uuid.UUID) -> ExperimentEligibility:
    reasons: list[str] = []
    with session_scope() as session:
        publication = session.get(Publication, publication_id)
        if publication is None:
            raise ValueError(f"publication not found: {publication_id}")
        if not publication.youtube_video_id:
            reasons.append("publication_not_on_youtube")
        profile = _publication_profile(session, publication)
        automation = active_automation(session, profile)
        if automation.level != AutomationLevel.AUTO_PUBLISH_SCHEDULED.value:
            reasons.append("automation_level_not_scheduled")
        snapshot = latest_packaging_intelligence(profile.id)
        if snapshot is None:
            reasons.append("packaging_intelligence_missing")
            return ExperimentEligibility(
                False,
                tuple(reasons),
                publication.id,
                None,
                None,
                None,
                automation.version,
                None,
                None,
            )
        recommendation = _recommendation_for_publication(
            snapshot,
            publication.id,
            recommendation_type="test",
        )
        if recommendation is None:
            reasons.append("no_test_recommendation")
            return ExperimentEligibility(
                False,
                tuple(reasons),
                publication.id,
                None,
                None,
                snapshot.id,
                automation.version,
                snapshot.maturity_days,
                None,
            )
        if list(recommendation.get("blockers") or []):
            reasons.append("recommendation_has_blockers")

        try:
            baseline_id = uuid.UUID(str(recommendation["baseline_variant_id"]))
            candidate_id = uuid.UUID(str(recommendation["candidate_variant_id"]))
        except (KeyError, ValueError):
            reasons.append("recommendation_lineage_invalid")
            baseline_id = None
            candidate_id = None

        if baseline_id is not None and candidate_id is not None:
            baseline = session.get(PublicationPackagingVariant, baseline_id)
            candidate = session.get(PublicationPackagingVariant, candidate_id)
            if baseline is None or baseline.publication_id != publication.id:
                reasons.append("baseline_variant_invalid")
            if candidate is None or candidate.publication_id != publication.id:
                reasons.append("candidate_variant_invalid")
            active_variant = _active_variant_id(publication)
            if active_variant != baseline_id:
                reasons.append("active_package_does_not_match_evidence_baseline")

        active_experiment = session.scalar(
            select(PackagingExperiment).where(
                PackagingExperiment.publication_id == publication.id,
                PackagingExperiment.status.in_(_ACTIVE_STATUSES),
            )
        )
        if active_experiment is not None:
            reasons.append("active_experiment_exists")

        latest_activation = _latest_mutation(session, publication.id)
        if latest_activation is not None:
            mutation_at = latest_activation.applied_at or latest_activation.created_at
            now = datetime.now(UTC)
            if mutation_at.tzinfo is None:
                mutation_at = mutation_at.replace(tzinfo=UTC)
            if now - mutation_at.astimezone(UTC) < _MIN_MUTATION_COOLDOWN:
                reasons.append("mutation_cooldown_active")

        return ExperimentEligibility(
            eligible=not reasons,
            reasons=tuple(sorted(set(reasons))),
            publication_id=publication.id,
            baseline_variant_id=baseline_id,
            candidate_variant_id=candidate_id,
            intelligence_snapshot_id=snapshot.id,
            automation_version=automation.version,
            maturity_days=snapshot.maturity_days,
            recommendation=recommendation,
        )


def start_packaging_experiment(
    publication_id: uuid.UUID,
) -> ExperimentAction:
    eligibility = experiment_eligibility(publication_id)
    if not eligibility.eligible:
        return ExperimentAction(
            experiment=None,
            activation=None,
            action="skipped",
            reason=",".join(eligibility.reasons) or "ineligible",
        )
    assert eligibility.baseline_variant_id is not None
    assert eligibility.candidate_variant_id is not None
    assert eligibility.intelligence_snapshot_id is not None
    assert eligibility.automation_version is not None
    assert eligibility.maturity_days is not None
    snapshot_id = eligibility.intelligence_snapshot_id
    key = (
        f"auto-{snapshot_id.hex[:12]}-"
        f"{eligibility.candidate_variant_id.hex[:12]}"
    )
    now = datetime.now(UTC)
    observe_after = now + timedelta(days=eligibility.maturity_days)

    with session_scope() as session:
        existing = session.scalar(
            select(PackagingExperiment).where(
                PackagingExperiment.publication_id == publication_id,
                PackagingExperiment.experiment_key == key,
            )
        )
        if existing is not None:
            session.expunge(existing)
            return ExperimentAction(
                experiment=existing,
                activation=None,
                action="reused",
                reason="experiment_key_already_registered",
            )
        experiment = PackagingExperiment(
            publication_id=publication_id,
            experiment_key=key,
            baseline_variant_id=eligibility.baseline_variant_id,
            candidate_variant_id=eligibility.candidate_variant_id,
            intelligence_snapshot_id=snapshot_id,
            automation_version=eligibility.automation_version,
            status="planned",
            stage="activation_registration",
            observe_after=observe_after,
            evidence_snapshot={
                "recommendation": eligibility.recommendation,
                "intelligence_snapshot_id": str(snapshot_id),
                "maturity_days": eligibility.maturity_days,
                "automation_version": eligibility.automation_version,
                "minimum_mutation_cooldown_hours": 24,
            },
        )
        session.add(experiment)
        session.flush()
        experiment_id = experiment.id

    try:
        activation = register_packaging_activation(
            publication_id,
            variant_id=eligibility.candidate_variant_id,
            activation_key=f"experiment-{experiment_id}-candidate",
        )
    except Exception as exc:
        with session_scope() as session:
            failed = session.get(PackagingExperiment, experiment_id)
            if failed is not None:
                failed.status = "failed"
                failed.stage = "activation_registration_failed"
                failed.error = str(exc)[:8000]
        raise

    with session_scope() as session:
        experiment = session.get(PackagingExperiment, experiment_id)
        if experiment is None:
            raise RuntimeError("packaging experiment disappeared after activation registration")
        experiment.start_activation_id = activation.id
        experiment.status = "activating"
        experiment.stage = "candidate_activation_queued"
        experiment.started_at = now
        session.add(
            DomainEvent(
                aggregate_type="publication",
                aggregate_id=str(publication_id),
                event_type="publication.packaging_experiment_started",
                payload={
                    "publication_id": str(publication_id),
                    "experiment_id": str(experiment.id),
                    "baseline_variant_id": str(experiment.baseline_variant_id),
                    "candidate_variant_id": str(experiment.candidate_variant_id),
                    "activation_id": str(activation.id),
                    "intelligence_snapshot_id": str(snapshot_id),
                    "automation_version": experiment.automation_version,
                    "observe_after": experiment.observe_after.isoformat(),
                },
            )
        )
        session.flush()
        session.refresh(experiment)
        session.expunge(experiment)
    return ExperimentAction(experiment, activation, "started", "eligible_test_recommendation")


def _latest_snapshot_for_experiment(
    experiment: PackagingExperiment,
) -> PackagingIntelligenceSnapshot | None:
    with session_scope() as session:
        publication = session.get(Publication, experiment.publication_id)
        if publication is None:
            return None
        profile = _publication_profile(session, publication)
    return latest_packaging_intelligence(profile.id)


def reconcile_packaging_experiment(
    experiment_id: uuid.UUID,
) -> ExperimentAction:
    now = datetime.now(UTC)
    with session_scope() as session:
        experiment = session.get(PackagingExperiment, experiment_id)
        if experiment is None:
            raise ValueError(f"packaging experiment not found: {experiment_id}")
        start_activation = (
            session.get(PublicationPackagingActivation, experiment.start_activation_id)
            if experiment.start_activation_id
            else None
        )
        rollback_activation = (
            session.get(PublicationPackagingActivation, experiment.rollback_activation_id)
            if experiment.rollback_activation_id
            else None
        )

        if experiment.status == "activating" and start_activation is not None:
            if start_activation.status == "failed":
                experiment.status = "failed"
                experiment.stage = "candidate_activation_failed"
                experiment.error = start_activation.error
            elif start_activation.status == "applied":
                experiment.status = "observing"
                experiment.stage = "candidate_observation"
                applied_at = start_activation.applied_at or now
                if applied_at.tzinfo is None:
                    applied_at = applied_at.replace(tzinfo=UTC)
                maturity_days = int(
                    (experiment.evidence_snapshot or {}).get("maturity_days") or 7
                )
                experiment.observe_after = applied_at.astimezone(UTC) + timedelta(
                    days=maturity_days
                )

        if experiment.status == "rollback_queued" and rollback_activation is not None:
            if rollback_activation.status == "failed":
                experiment.status = "failed"
                experiment.stage = "rollback_failed"
                experiment.error = rollback_activation.error
            elif rollback_activation.status == "applied":
                experiment.status = "rolled_back"
                experiment.stage = "rollback_applied"
                experiment.completed_at = now

        session.flush()
        if experiment.status != "observing" or now < experiment.observe_after:
            session.refresh(experiment)
            session.expunge(experiment)
            return ExperimentAction(experiment, None, "observe", experiment.stage)
        detached_id = experiment.id
        session.expunge(experiment)

    with session_scope() as session:
        experiment = session.get(PackagingExperiment, detached_id)
        if experiment is None:
            raise RuntimeError("packaging experiment disappeared during reconciliation")
        snapshot = _latest_snapshot_for_experiment(experiment)
        if snapshot is None:
            session.expunge(experiment)
            return ExperimentAction(experiment, None, "observe", "intelligence_missing")
        recommendation = _recommendation_for_publication(
            snapshot,
            experiment.publication_id,
            candidate_variant_id=experiment.candidate_variant_id,
        )
        if recommendation is None or recommendation.get("candidate_window_id") is None:
            session.expunge(experiment)
            return ExperimentAction(experiment, None, "observe", "mature_candidate_evidence_missing")
        blockers = set(recommendation.get("blockers") or [])
        if recommendation.get("recommendation_type") == "prefer" and not blockers:
            experiment.status = "completed"
            experiment.stage = "candidate_preferred"
            experiment.completed_at = now
            experiment.evidence_snapshot = {
                **dict(experiment.evidence_snapshot or {}),
                "completion_snapshot_id": str(snapshot.id),
                "completion_recommendation": recommendation,
            }
            session.add(
                DomainEvent(
                    aggregate_type="publication",
                    aggregate_id=str(experiment.publication_id),
                    event_type="publication.packaging_experiment_completed",
                    payload={
                        "publication_id": str(experiment.publication_id),
                        "experiment_id": str(experiment.id),
                        "candidate_variant_id": str(experiment.candidate_variant_id),
                        "result": "preferred",
                        "intelligence_snapshot_id": str(snapshot.id),
                    },
                )
            )
            session.flush()
            session.refresh(experiment)
            session.expunge(experiment)
            return ExperimentAction(experiment, None, "completed", "candidate_preferred")

        harmful = blockers & _ROLLBACK_BLOCKERS
        if not harmful:
            session.expunge(experiment)
            return ExperimentAction(experiment, None, "observe", "no_harm_guardrail_triggered")
        publication_id = experiment.publication_id
        baseline_variant_id = experiment.baseline_variant_id
        experiment_key = experiment.experiment_key

    activation = register_packaging_activation(
        publication_id,
        variant_id=baseline_variant_id,
        activation_key=f"{experiment_key}-rollback",
    )
    with session_scope() as session:
        experiment = session.get(PackagingExperiment, detached_id)
        if experiment is None:
            raise RuntimeError("packaging experiment disappeared before rollback persistence")
        experiment.rollback_activation_id = activation.id
        experiment.status = "rollback_queued"
        experiment.stage = "guardrail_rollback_queued"
        experiment.evidence_snapshot = {
            **dict(experiment.evidence_snapshot or {}),
            "rollback_snapshot_id": str(snapshot.id),
            "rollback_recommendation": recommendation,
            "rollback_blockers": sorted(harmful),
        }
        session.add(
            DomainEvent(
                aggregate_type="publication",
                aggregate_id=str(publication_id),
                event_type="publication.packaging_experiment_rollback_queued",
                payload={
                    "publication_id": str(publication_id),
                    "experiment_id": str(experiment.id),
                    "rollback_activation_id": str(activation.id),
                    "baseline_variant_id": str(baseline_variant_id),
                    "blockers": sorted(harmful),
                },
            )
        )
        session.flush()
        session.refresh(experiment)
        session.expunge(experiment)
    return ExperimentAction(experiment, activation, "rollback", ",".join(sorted(harmful)))


def list_packaging_experiments(
    publication_id: uuid.UUID,
) -> list[PackagingExperiment]:
    with session_scope() as session:
        if session.get(Publication, publication_id) is None:
            raise ValueError(f"publication not found: {publication_id}")
        rows = list(
            session.scalars(
                select(PackagingExperiment)
                .where(PackagingExperiment.publication_id == publication_id)
                .order_by(PackagingExperiment.created_at.desc())
            )
        )
        for row in rows:
            session.expunge(row)
        return rows


def channel_experiment_candidates(
    channel_profile_id: uuid.UUID,
) -> list[uuid.UUID]:
    snapshot = latest_packaging_intelligence(channel_profile_id)
    if snapshot is None:
        return []
    ids: list[uuid.UUID] = []
    for raw in snapshot.recommendations or []:
        row = dict(raw)
        if row.get("recommendation_type") != "test":
            continue
        if list(row.get("blockers") or []):
            continue
        try:
            ids.append(uuid.UUID(str(row["publication_id"])))
        except (KeyError, ValueError):
            continue
    return sorted(set(ids), key=str)


def active_channel_experiments(
    channel_profile_id: uuid.UUID,
) -> list[PackagingExperiment]:
    with session_scope() as session:
        profile = ensure_active_profile(session, channel_profile_id)
        publication_ids = list(
            session.scalars(
                select(Publication.id).where(
                    Publication.youtube_connection_id == profile.youtube_connection_id
                )
            )
        )
        if not publication_ids:
            return []
        rows = list(
            session.scalars(
                select(PackagingExperiment).where(
                    PackagingExperiment.publication_id.in_(publication_ids),
                    PackagingExperiment.status.in_(_ACTIVE_STATUSES),
                )
            )
        )
        for row in rows:
            session.expunge(row)
        return rows
