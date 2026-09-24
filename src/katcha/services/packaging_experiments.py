from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select

from katcha.db import session_scope
from katcha.domain import AutomationLevel, ChannelStatus, PublicationStatus
from katcha.intelligence_models import AutomationPolicyVersion, ChannelProfile
from katcha.models import DomainEvent
from katcha.packaging_intelligence_models import (
    PackagingIntelligenceSnapshot,
    PackagingVariantPerformanceWindow,
)
from katcha.packaging_models import (
    PackagingExperiment,
    PublicationPackagingActivation,
    PublicationPackagingVariant,
)
from katcha.publishing_models import Publication
from katcha.services.packaging import register_packaging_activation

PACIFIC = ZoneInfo("America/Los_Angeles")
ACTIVE = ("preparing", "pending", "observing", "rollback_pending")
COOLDOWN_DAYS = 7


def _utc(value: datetime | None = None) -> datetime:
    value = value or datetime.now(UTC)
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _day(value: datetime) -> date:
    return _utc(value).astimezone(PACIFIC).date()


def _latest_snapshot(session: Any, channel_id: uuid.UUID) -> PackagingIntelligenceSnapshot | None:
    return session.scalar(
        select(PackagingIntelligenceSnapshot)
        .where(
            PackagingIntelligenceSnapshot.channel_profile_id == channel_id,
        )
        .order_by(
            PackagingIntelligenceSnapshot.created_at.desc(),
            PackagingIntelligenceSnapshot.version.desc(),
        )
        .limit(1)
    )


def _recent_activation(
    session: Any, publication_id: uuid.UUID
) -> PublicationPackagingActivation | None:
    return session.scalar(
        select(PublicationPackagingActivation)
        .where(
            PublicationPackagingActivation.publication_id == publication_id,
        )
        .order_by(
            PublicationPackagingActivation.created_at.desc(),
            PublicationPackagingActivation.id.desc(),
        )
        .limit(1)
    )


def _eligibility(
    session: Any, publication_id: uuid.UUID, candidate_id: uuid.UUID, now: datetime
) -> dict[str, Any]:
    publication = session.get(Publication, publication_id)
    if publication is None:
        raise ValueError(f"publication not found: {publication_id}")
    channel = session.scalar(
        select(ChannelProfile).where(
            ChannelProfile.youtube_connection_id == publication.youtube_connection_id,
        )
    )
    if channel is None or channel.status != ChannelStatus.ACTIVE.value:
        return {"eligible": False, "reason": "channel_inactive"}
    policy = session.scalar(
        select(AutomationPolicyVersion).where(
            AutomationPolicyVersion.channel_profile_id == channel.id,
            AutomationPolicyVersion.version == channel.active_automation_version,
        )
    )
    if policy is None or policy.level != AutomationLevel.AUTO_PUBLISH_SCHEDULED.value:
        return {"eligible": False, "reason": "automation_policy_disallows_mutation"}
    if publication.status != PublicationStatus.PUBLISHED.value or not publication.youtube_video_id:
        return {"eligible": False, "reason": "publication_not_published"}
    candidate = session.get(PublicationPackagingVariant, candidate_id)
    if candidate is None or candidate.publication_id != publication_id:
        return {"eligible": False, "reason": "candidate_channel_mismatch"}
    active = session.scalar(
        select(PackagingExperiment.id)
        .where(
            PackagingExperiment.publication_id == publication_id,
            PackagingExperiment.status.in_(ACTIVE),
        )
        .limit(1)
    )
    if active is not None:
        return {"eligible": False, "reason": "experiment_already_active"}
    recent = _recent_activation(session, publication_id)
    if recent is None or recent.status != "applied":
        return {"eligible": False, "reason": "previous_package_not_applied"}
    previous = session.get(PublicationPackagingVariant, recent.variant_id)
    if previous is None or previous.publication_id != publication_id or previous.id == candidate.id:
        return {"eligible": False, "reason": "previous_package_invalid"}
    if _day(_utc(recent.applied_at or recent.created_at)) + timedelta(days=COOLDOWN_DAYS) > _day(
        now
    ):
        return {"eligible": False, "reason": "activation_cooldown"}
    if candidate.thumbnail_storage_key:
        metadata = dict(candidate.variant_metadata or {})
        if not (
            candidate.created_by == "katcha-thumbnail-renderer"
            and candidate.thumbnail_sha256
            and candidate.thumbnail_size_bytes
            and metadata.get("thumbnail_parent_variant_id")
            and metadata.get("thumbnail_render_manifest")
            and metadata.get("thumbnail_render_verification")
        ):
            return {"eligible": False, "reason": "thumbnail_lineage_incomplete"}
        if not previous.thumbnail_sha256:
            return {"eligible": False, "reason": "rollback_thumbnail_unavailable"}
    snapshot = _latest_snapshot(session, channel.id)
    if snapshot is None or _utc(snapshot.created_at) < now - timedelta(hours=30):
        return {"eligible": False, "reason": "packaging_evidence_stale"}
    if snapshot.validation_metrics.get("provider_error_count", 0):
        return {"eligible": False, "reason": "packaging_provider_degraded"}
    for recommendation in snapshot.recommendations or []:
        if not (
            recommendation.get("recommendation_type") == "test"
            and not recommendation.get("blockers")
            and recommendation.get("publication_id") == str(publication_id)
            and recommendation.get("candidate_variant_id") == str(candidate_id)
            and recommendation.get("baseline_variant_id") == str(previous.id)
            and recommendation.get("candidate_window_id") is None
        ):
            continue
        try:
            window_id = uuid.UUID(str(recommendation["baseline_window_id"]))
        except (KeyError, ValueError, TypeError):
            continue
        baseline = session.get(PackagingVariantPerformanceWindow, window_id)
        if (
            baseline is None
            or baseline.publication_id != publication_id
            or baseline.packaging_variant_id != previous.id
            or baseline.maturity_days < 7
            or baseline.impressions is None
            or baseline.impressions < 1000
            or baseline.ctr is None
            or baseline.average_view_percentage is None
            or baseline.retention_50 is None
            or baseline.window_end > _day(now) - timedelta(days=2)
            or baseline.window_end < _day(now) - timedelta(days=45)
        ):
            continue
        return {
            "eligible": True,
            "reason": "mature_test_recommendation",
            "channel_profile_id": str(channel.id),
            "policy_version": policy.version,
            "snapshot_id": str(snapshot.id),
            "baseline_window_id": str(window_id),
            "previous_variant_id": str(previous.id),
            "candidate_variant_id": str(candidate.id),
            "candidate_thumbnail_sha256": candidate.thumbnail_sha256,
            "previous_thumbnail_sha256": previous.thumbnail_sha256,
        }
    return {"eligible": False, "reason": "mature_test_evidence_missing"}


def experiment_eligibility(
    publication_id: uuid.UUID, candidate_id: uuid.UUID, *, now: datetime | None = None
) -> dict[str, Any]:
    with session_scope() as session:
        return _eligibility(session, publication_id, candidate_id, _utc(now))


def start_packaging_experiment(
    publication_id: uuid.UUID,
    candidate_id: uuid.UUID,
    *,
    now: datetime | None = None,
    expected_channel_id: uuid.UUID | None = None,
) -> PackagingExperiment:
    reference = _utc(now)
    with session_scope() as session:
        # Serialize competing candidate claims for a publication on PostgreSQL.
        publication = session.scalar(
            select(Publication).where(Publication.id == publication_id).with_for_update()
        )
        if publication is None:
            raise ValueError(f"publication not found: {publication_id}")
        if expected_channel_id is not None:
            channel = session.get(ChannelProfile, expected_channel_id)
            if (
                channel is None
                or channel.youtube_connection_id != publication.youtube_connection_id
            ):
                raise ValueError("candidate belongs to another channel")
        current = session.scalar(
            select(PackagingExperiment)
            .where(
                PackagingExperiment.publication_id == publication_id,
                PackagingExperiment.status.in_(ACTIVE),
            )
            .limit(1)
        )
        if current is not None:
            if current.candidate_variant_id != candidate_id:
                raise ValueError("a different packaging experiment is already active")
            row = current
        else:
            evidence = _eligibility(session, publication_id, candidate_id, reference)
            if not evidence["eligible"]:
                raise ValueError(str(evidence["reason"]))
            key = f"snapshot-{evidence['snapshot_id']}-candidate-{candidate_id}"
            existing = session.scalar(
                select(PackagingExperiment).where(
                    PackagingExperiment.publication_id == publication_id,
                    PackagingExperiment.experiment_key == key,
                )
            )
            if existing is not None:
                row = existing
            else:
                row = PackagingExperiment(
                    publication_id=publication_id,
                    channel_profile_id=uuid.UUID(evidence["channel_profile_id"]),
                    experiment_key=key,
                    status="preparing",
                    candidate_variant_id=candidate_id,
                    previous_variant_id=uuid.UUID(evidence["previous_variant_id"]),
                    snapshot_id=uuid.UUID(evidence["snapshot_id"]),
                    baseline_window_id=uuid.UUID(evidence["baseline_window_id"]),
                    evidence={**evidence, "started_at": reference.isoformat()},
                    created_at=reference,
                )
                session.add(row)
                session.flush()
                session.add(
                    DomainEvent(
                        aggregate_type="packaging_experiment",
                        aggregate_id=str(row.id),
                        event_type="packaging.experiment.started",
                        payload={
                            "experiment_id": str(row.id),
                            "publication_id": str(publication_id),
                            "channel_profile_id": str(row.channel_profile_id),
                            "candidate_variant_id": str(candidate_id),
                            "previous_variant_id": str(row.previous_variant_id),
                            "snapshot_id": str(row.snapshot_id),
                            "baseline_window_id": str(row.baseline_window_id),
                        },
                    )
                )
        session.expunge(row)
    return reconcile_packaging_experiment(row.id, now=reference)


def _register_experiment_activation(row: PackagingExperiment) -> PackagingExperiment:
    activation = register_packaging_activation(
        row.publication_id,
        variant_id=row.candidate_variant_id,
        activation_key=f"experiment-{row.id}",
    )
    with session_scope() as session:
        stored = session.get(PackagingExperiment, row.id)
        if stored is None:
            raise ValueError("packaging experiment disappeared")
        if stored.status == "preparing":
            stored.activation_id = activation.id
            stored.status = "pending"
        session.flush()
        session.expunge(stored)
        return stored


def _rollback(
    row: PackagingExperiment, *, reason: str, snapshot_id: str | None
) -> PackagingExperiment:
    # The experiment remains active while P9.1 reconciles the prior immutable variant.
    with session_scope() as session:
        stored = session.scalar(
            select(PackagingExperiment).where(PackagingExperiment.id == row.id).with_for_update()
        )
        if stored is None:
            raise ValueError("packaging experiment disappeared")
        if stored.status != "rollback_pending":
            stored.status = "rollback_pending"
            stored.evidence = {
                **dict(stored.evidence or {}),
                "rollback_reason": reason,
                "rollback_snapshot_id": snapshot_id,
            }
            session.add(
                DomainEvent(
                    aggregate_type="packaging_experiment",
                    aggregate_id=str(row.id),
                    event_type="packaging.experiment.rollback_requested",
                    payload={
                        "experiment_id": str(row.id),
                        "reason": reason,
                        "snapshot_id": snapshot_id,
                    },
                )
            )
    activation = register_packaging_activation(
        row.publication_id,
        variant_id=row.previous_variant_id,
        activation_key=f"experiment-rollback-{row.id}",
    )
    with session_scope() as session:
        stored = session.get(PackagingExperiment, row.id)
        if stored is None:
            raise ValueError("packaging experiment disappeared")
        stored.rollback_activation_id = activation.id
        session.flush()
        session.expunge(stored)
        return stored


def _automatic_policy_allows(channel_profile_id: uuid.UUID) -> bool:
    with session_scope() as session:
        channel = session.get(ChannelProfile, channel_profile_id)
        if channel is None or channel.status != ChannelStatus.ACTIVE.value:
            return False
        policy = session.scalar(
            select(AutomationPolicyVersion).where(
                AutomationPolicyVersion.channel_profile_id == channel.id,
                AutomationPolicyVersion.version == channel.active_automation_version,
            )
        )
        return policy is not None and policy.level == AutomationLevel.AUTO_PUBLISH_SCHEDULED.value


def reconcile_packaging_experiment(
    experiment_id: uuid.UUID, *, now: datetime | None = None
) -> PackagingExperiment:
    reference = _utc(now)
    row = get_packaging_experiment(experiment_id)
    if row.status == "preparing":
        return _register_experiment_activation(row)
    if row.status not in ACTIVE:
        return row
    with session_scope() as session:
        activation = session.get(PublicationPackagingActivation, row.activation_id)
        if activation is None:
            raise ValueError("experiment activation lineage is missing")
        rollback = (
            session.get(PublicationPackagingActivation, row.rollback_activation_id)
            if row.rollback_activation_id
            else None
        )
        snapshot = _latest_snapshot(session, row.channel_profile_id)
        snapshot_id = str(snapshot.id) if snapshot else None
        recommendation: dict[str, Any] | None = None
        if (
            activation.applied_at
            and snapshot is not None
            and snapshot.id != row.snapshot_id
            and _utc(snapshot.created_at) > _utc(activation.applied_at)
            and not snapshot.validation_metrics.get("provider_error_count", 0)
        ):
            for item in snapshot.recommendations or []:
                if (
                    item.get("publication_id") == str(row.publication_id)
                    and item.get("baseline_variant_id") == str(row.previous_variant_id)
                    and item.get("candidate_variant_id") == str(row.candidate_variant_id)
                    and item.get("candidate_window_id")
                ):
                    candidate_window = session.get(
                        PackagingVariantPerformanceWindow,
                        uuid.UUID(str(item["candidate_window_id"])),
                    )
                    if (
                        candidate_window is not None
                        and candidate_window.publication_id == row.publication_id
                        and candidate_window.packaging_variant_id == row.candidate_variant_id
                        and candidate_window.window_start > _day(_utc(activation.applied_at))
                        and candidate_window.window_end <= _day(reference) - timedelta(days=2)
                        and candidate_window.maturity_days >= 7
                    ):
                        recommendation = item
                        break
    if row.status == "rollback_pending":
        if not _automatic_policy_allows(row.channel_profile_id):
            return _transition(row, "needs_review", snapshot_id=snapshot_id)
        if rollback is None:
            return _rollback(
                row, reason=str(row.evidence.get("rollback_reason")), snapshot_id=snapshot_id
            )
        if rollback.status in {"applied", "failed"}:
            outcome = "rolled_back" if rollback.status == "applied" else "needs_review"
            return _transition(row, outcome, snapshot_id=snapshot_id)
        return row
    if activation.status == "failed":
        if not _automatic_policy_allows(row.channel_profile_id):
            return _transition(row, "needs_review", snapshot_id=snapshot_id)
        return _rollback(row, reason="activation_failed", snapshot_id=snapshot_id)
    if activation.status != "applied":
        return row
    if recommendation is not None:
        blockers = set(recommendation.get("blockers") or [])
        if blockers & {
            "watch_percentage_degraded",
            "midpoint_retention_degraded",
            "publication_margin_negative",
        }:
            if not _automatic_policy_allows(row.channel_profile_id):
                return _transition(row, "needs_review", snapshot_id=snapshot_id)
            return _rollback(row, reason="mature_guardrail_deterioration", snapshot_id=snapshot_id)
        if recommendation.get("recommendation_type") == "prefer" and not blockers:
            return _transition(row, "completed", snapshot_id=snapshot_id)
    if row.status == "pending":
        return _transition(row, "observing", snapshot_id=snapshot_id)
    return row


def _transition(
    row: PackagingExperiment, status: str, *, snapshot_id: str | None
) -> PackagingExperiment:
    with session_scope() as session:
        stored = session.scalar(
            select(PackagingExperiment).where(PackagingExperiment.id == row.id).with_for_update()
        )
        if stored is None:
            raise ValueError("packaging experiment disappeared")
        if stored.status != status:
            stored.status = status
            stored.evidence = {
                **dict(stored.evidence or {}),
                "last_observation_snapshot_id": snapshot_id,
            }
            session.add(
                DomainEvent(
                    aggregate_type="packaging_experiment",
                    aggregate_id=str(row.id),
                    event_type=f"packaging.experiment.{status}",
                    payload={
                        "experiment_id": str(row.id),
                        "publication_id": str(row.publication_id),
                        "snapshot_id": snapshot_id,
                    },
                )
            )
        session.flush()
        session.expunge(stored)
        return stored


def get_packaging_experiment(experiment_id: uuid.UUID) -> PackagingExperiment:
    with session_scope() as session:
        row = session.get(PackagingExperiment, experiment_id)
        if row is None:
            raise ValueError(f"packaging experiment not found: {experiment_id}")
        session.expunge(row)
        return row


def list_packaging_experiments(publication_id: uuid.UUID) -> list[PackagingExperiment]:
    with session_scope() as session:
        if session.get(Publication, publication_id) is None:
            raise ValueError(f"publication not found: {publication_id}")
        rows = list(
            session.scalars(
                select(PackagingExperiment)
                .where(
                    PackagingExperiment.publication_id == publication_id,
                )
                .order_by(PackagingExperiment.created_at.desc())
            )
        )
        for row in rows:
            session.expunge(row)
        return rows
