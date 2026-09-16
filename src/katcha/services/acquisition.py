from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from katcha.acquisition.policy import evaluate_acquisition_policy
from katcha.acquisition_models import (
    DiscoveryCandidate,
    DiscoveryRun,
    RightsAssessment,
    RightsEvidence,
)
from katcha.db import session_scope
from katcha.domain import (
    AudioRightsStatus,
    DiscoveryCandidateStatus,
    DiscoveryRunStatus,
    GateStatus,
    RightsBasis,
    RightsLane,
)
from katcha.integrations.download import canonicalize_url, detect_platform
from katcha.models import DomainEvent, SourceItem
from katcha.services.sources import register_source

_EVIDENCE_TYPES: dict[RightsBasis, set[str]] = {
    RightsBasis.DIRECT_PERMISSION: {
        "direct_permission",
        "creator_permission",
        "permission_record",
    },
    RightsBasis.LICENSED: {
        "license",
        "license_agreement",
        "license_terms",
    },
    RightsBasis.CC0: {"cc0", "license_page", "license_terms"},
    RightsBasis.CC_BY: {"cc_by", "license_page", "license_terms"},
    RightsBasis.PUBLIC_DOMAIN: {
        "public_domain",
        "public_domain_record",
        "source_page",
    },
}


@dataclass(frozen=True, slots=True)
class ClipAcquisitionState:
    managed: bool
    eligible: bool
    candidate_id: uuid.UUID | None
    assessment_id: uuid.UUID | None
    rights_lane: str | None
    reason: str


def _run_key(
    adapter_key: str,
    adapter_version: str,
    query: dict[str, Any],
    idempotency_key: str | None,
) -> str:
    if idempotency_key and idempotency_key.strip():
        return idempotency_key.strip()
    payload = json.dumps(
        {
            "adapter": adapter_key,
            "version": adapter_version,
            "query": query,
            "nonce": uuid.uuid4().hex,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode()).hexdigest()[:40]


def register_discovery_run(
    *,
    adapter_key: str,
    adapter_version: str,
    query: dict[str, Any] | None = None,
    idempotency_key: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> DiscoveryRun:
    key = _run_key(
        adapter_key,
        adapter_version,
        query or {},
        idempotency_key,
    )
    with session_scope() as session:
        existing = session.scalar(
            select(DiscoveryRun).where(
                DiscoveryRun.adapter_key == adapter_key,
                DiscoveryRun.run_key == key,
            )
        )
        if existing is not None:
            session.expunge(existing)
            return existing
        run = DiscoveryRun(
            adapter_key=adapter_key,
            adapter_version=adapter_version,
            run_key=key,
            status=DiscoveryRunStatus.QUEUED.value,
            query=query or {},
            cursor={},
            run_metadata=metadata or {},
        )
        session.add(run)
        session.flush()
        session.add(
            DomainEvent(
                aggregate_type="discovery_run",
                aggregate_id=str(run.id),
                event_type="discovery_run.created",
                payload={
                    "discovery_run_id": str(run.id),
                    "adapter_key": adapter_key,
                    "adapter_version": adapter_version,
                    "run_key": key,
                },
            )
        )
        session.refresh(run)
        session.expunge(run)
        return run


def register_discovery_candidate(
    *,
    source_url: str,
    adapter_key: str,
    discovery_run_id: uuid.UUID | None = None,
    external_id: str | None = None,
    title: str | None = None,
    creator: str | None = None,
    creator_url: str | None = None,
    provenance_confidence: float = 0.0,
    provenance_claims: dict[str, Any] | None = None,
    metadata: dict[str, Any] | None = None,
) -> DiscoveryCandidate:
    canonical = canonicalize_url(source_url)
    confidence = max(0.0, min(float(provenance_confidence), 1.0))
    normalized_external_id = external_id.strip() if external_id else None
    with session_scope() as session:
        if (
            discovery_run_id is not None
            and session.get(DiscoveryRun, discovery_run_id) is None
        ):
            raise ValueError(f"discovery run not found: {discovery_run_id}")
        existing = session.scalar(
            select(DiscoveryCandidate).where(
                DiscoveryCandidate.canonical_url == canonical
            )
        )
        if existing is None and normalized_external_id:
            existing = session.scalar(
                select(DiscoveryCandidate).where(
                    DiscoveryCandidate.adapter_key == adapter_key,
                    DiscoveryCandidate.external_id == normalized_external_id,
                )
            )
        if existing is not None:
            session.expunge(existing)
            return existing

        candidate = DiscoveryCandidate(
            discovery_run_id=discovery_run_id,
            adapter_key=adapter_key,
            external_id=normalized_external_id,
            source_url=source_url,
            canonical_url=canonical,
            platform=detect_platform(canonical),
            status=DiscoveryCandidateStatus.DISCOVERED.value,
            title=title,
            creator=creator,
            creator_url=creator_url,
            provenance_confidence=Decimal(str(confidence)),
            provenance_claims=provenance_claims or {},
            candidate_metadata=metadata or {},
        )
        session.add(candidate)
        session.flush()
        session.add(
            DomainEvent(
                aggregate_type="discovery_candidate",
                aggregate_id=str(candidate.id),
                event_type="discovery_candidate.discovered",
                payload={
                    "discovery_candidate_id": str(candidate.id),
                    "discovery_run_id": (
                        str(discovery_run_id) if discovery_run_id else None
                    ),
                    "adapter_key": adapter_key,
                    "platform": candidate.platform,
                    "status": candidate.status,
                },
            )
        )
        session.refresh(candidate)
        session.expunge(candidate)
        return candidate


def latest_rights_assessment(
    session: Session,
    candidate_id: uuid.UUID,
) -> RightsAssessment | None:
    return session.scalar(
        select(RightsAssessment)
        .where(RightsAssessment.discovery_candidate_id == candidate_id)
        .order_by(RightsAssessment.version.desc())
        .limit(1)
    )


def _matching_evidence_present(
    session: Session,
    candidate_id: uuid.UUID,
    rights_basis: RightsBasis,
) -> bool:
    accepted = _EVIDENCE_TYPES.get(rights_basis)
    if not accepted:
        return False
    evidence_types = {
        str(value).strip().casefold()
        for value in session.scalars(
            select(RightsEvidence.evidence_type)
            .join(
                RightsAssessment,
                RightsEvidence.rights_assessment_id == RightsAssessment.id,
            )
            .where(RightsAssessment.discovery_candidate_id == candidate_id)
        )
    }
    return bool(evidence_types & accepted)


def assess_discovery_candidate(
    candidate_id: uuid.UUID,
    *,
    rights_basis: RightsBasis,
    audio_status: AudioRightsStatus,
    originality_gate: GateStatus,
    risk_flags: list[str] | None = None,
    operator_authorized: bool = False,
    fair_use_factors: dict[str, Any] | None = None,
    metadata: dict[str, Any] | None = None,
    actor: str = "operator",
    reason: str | None = None,
) -> RightsAssessment:
    with session_scope() as session:
        candidate = session.get(DiscoveryCandidate, candidate_id)
        if candidate is None:
            raise ValueError(f"discovery candidate not found: {candidate_id}")
        evidence_present = _matching_evidence_present(
            session,
            candidate_id,
            rights_basis,
        )
        policy = evaluate_acquisition_policy(
            rights_basis=rights_basis,
            audio_status=audio_status,
            originality_gate=originality_gate,
            risk_flags=risk_flags or [],
            operator_authorized=operator_authorized,
            evidence_present=evidence_present,
        )
        version = int(
            session.scalar(
                select(func.coalesce(func.max(RightsAssessment.version), 0)).where(
                    RightsAssessment.discovery_candidate_id == candidate_id
                )
            )
            or 0
        ) + 1
        assessment_metadata = dict(metadata or {})
        assessment_metadata["policy_reasons"] = list(policy.reasons)
        assessment_metadata["policy_advisories"] = list(policy.advisories)
        assessment_metadata["policy_version"] = "acquisition-policy-v1"
        assessment_metadata["matching_rights_evidence_present"] = evidence_present
        assessment = RightsAssessment(
            discovery_candidate_id=candidate_id,
            version=version,
            rights_basis=rights_basis.value,
            rights_lane=policy.rights_lane.value,
            rights_gate=policy.rights_gate.value,
            audio_status=audio_status.value,
            originality_gate=originality_gate.value,
            production_eligible=policy.production_eligible,
            operator_authorized=operator_authorized,
            risk_flags=list(dict.fromkeys(risk_flags or [])),
            fair_use_factors=fair_use_factors or {},
            assessment_metadata=assessment_metadata,
            actor=actor,
            reason=reason,
        )
        session.add(assessment)
        if policy.production_eligible:
            candidate.status = DiscoveryCandidateStatus.QUALIFIED.value
        elif policy.rights_lane == RightsLane.RED:
            candidate.status = DiscoveryCandidateStatus.REJECTED.value
        else:
            candidate.status = DiscoveryCandidateStatus.REVIEW.value
        session.flush()
        session.add(
            DomainEvent(
                aggregate_type="discovery_candidate",
                aggregate_id=str(candidate.id),
                event_type="discovery_candidate.assessed",
                payload={
                    "discovery_candidate_id": str(candidate.id),
                    "rights_assessment_id": str(assessment.id),
                    "version": version,
                    "rights_basis": assessment.rights_basis,
                    "rights_lane": assessment.rights_lane,
                    "rights_gate": assessment.rights_gate,
                    "audio_status": assessment.audio_status,
                    "originality_gate": assessment.originality_gate,
                    "production_eligible": assessment.production_eligible,
                    "matching_rights_evidence_present": evidence_present,
                    "status": candidate.status,
                },
            )
        )
        session.refresh(assessment)
        session.expunge(assessment)
        return assessment


def add_rights_evidence(
    assessment_id: uuid.UUID,
    *,
    evidence_type: str,
    source_url: str | None = None,
    snapshot_key: str | None = None,
    content_sha256: str | None = None,
    terms_version: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> RightsEvidence:
    digest = content_sha256.casefold() if content_sha256 else None
    if digest is not None:
        valid_digest = len(digest) == 64 and all(
            ch in "0123456789abcdef" for ch in digest
        )
        if not valid_digest:
            raise ValueError("content_sha256 must be a 64-character hexadecimal digest")
    with session_scope() as session:
        assessment = session.get(RightsAssessment, assessment_id)
        if assessment is None:
            raise ValueError(f"rights assessment not found: {assessment_id}")
        evidence = RightsEvidence(
            rights_assessment_id=assessment_id,
            evidence_type=evidence_type.strip().casefold(),
            source_url=source_url,
            snapshot_key=snapshot_key,
            content_sha256=digest,
            terms_version=terms_version,
            evidence_metadata=metadata or {},
        )
        session.add(evidence)
        session.flush()
        session.add(
            DomainEvent(
                aggregate_type="discovery_candidate",
                aggregate_id=str(assessment.discovery_candidate_id),
                event_type="discovery_candidate.rights_evidence_added",
                payload={
                    "discovery_candidate_id": str(assessment.discovery_candidate_id),
                    "rights_assessment_id": str(assessment.id),
                    "rights_evidence_id": str(evidence.id),
                    "evidence_type": evidence.evidence_type,
                    "reassessment_required_for_gate_change": True,
                },
            )
        )
        session.refresh(evidence)
        session.expunge(evidence)
        return evidence


def promote_discovery_candidate(
    candidate_id: uuid.UUID,
    *,
    actor: str = "operator",
) -> SourceItem:
    with session_scope() as session:
        candidate = session.get(DiscoveryCandidate, candidate_id)
        if candidate is None:
            raise ValueError(f"discovery candidate not found: {candidate_id}")
        if candidate.source_item_id is not None:
            source = session.get(SourceItem, candidate.source_item_id)
            if source is None:
                raise RuntimeError("promoted candidate source item disappeared")
            session.expunge(source)
            return source
        assessment = latest_rights_assessment(session, candidate.id)
        if assessment is None:
            raise ValueError("candidate has no rights assessment")
        if not assessment.production_eligible:
            raise ValueError(
                "candidate cannot be promoted until rights, audio, and originality gates clear"
            )
        canonical_url = candidate.canonical_url
        assessment_id = assessment.id
        assessment_version = assessment.version
        rights_basis = assessment.rights_basis
        rights_lane = assessment.rights_lane

    source = register_source(canonical_url)
    with session_scope() as session:
        candidate = session.get(DiscoveryCandidate, candidate_id)
        managed_source = session.get(SourceItem, source.id)
        if candidate is None or managed_source is None:
            raise RuntimeError("candidate/source disappeared during promotion")
        if (
            candidate.source_item_id is not None
            and candidate.source_item_id != managed_source.id
        ):
            raise RuntimeError("candidate was concurrently promoted to a different source")
        managed_source.source_metadata = {
            **dict(managed_source.source_metadata or {}),
            "acquisition_managed": True,
            "discovery_candidate_id": str(candidate.id),
            "rights_assessment_id": str(assessment_id),
            "rights_assessment_version": assessment_version,
            "rights_basis": rights_basis,
            "rights_lane": rights_lane,
        }
        candidate.source_item_id = managed_source.id
        candidate.status = DiscoveryCandidateStatus.PROMOTED.value
        session.add(
            DomainEvent(
                aggregate_type="discovery_candidate",
                aggregate_id=str(candidate.id),
                event_type="discovery_candidate.promoted",
                payload={
                    "discovery_candidate_id": str(candidate.id),
                    "source_item_id": str(managed_source.id),
                    "rights_assessment_id": str(assessment_id),
                    "rights_lane": rights_lane,
                    "actor": actor,
                },
            )
        )
        session.flush()
        session.refresh(managed_source)
        session.expunge(managed_source)
        return managed_source


def clip_acquisition_state(clip_id: uuid.UUID) -> ClipAcquisitionState:
    with session_scope() as session:
        candidates = list(
            session.scalars(
                select(DiscoveryCandidate)
                .join(SourceItem, DiscoveryCandidate.source_item_id == SourceItem.id)
                .where(SourceItem.clip_id == clip_id)
                .order_by(DiscoveryCandidate.discovered_at.asc())
            )
        )
        if not candidates:
            return ClipAcquisitionState(
                managed=False,
                eligible=True,
                candidate_id=None,
                assessment_id=None,
                rights_lane=None,
                reason="legacy_or_operator_ingest_unmanaged",
            )
        latest_rows: list[tuple[DiscoveryCandidate, RightsAssessment | None]] = [
            (candidate, latest_rights_assessment(session, candidate.id))
            for candidate in candidates
        ]
        eligible = [
            (candidate, assessment)
            for candidate, assessment in latest_rows
            if assessment is not None and assessment.production_eligible
        ]
        if eligible:
            candidate, assessment = eligible[0]
            assert assessment is not None
            return ClipAcquisitionState(
                managed=True,
                eligible=True,
                candidate_id=candidate.id,
                assessment_id=assessment.id,
                rights_lane=assessment.rights_lane,
                reason="managed_assessment_cleared",
            )
        candidate, assessment = latest_rows[0]
        return ClipAcquisitionState(
            managed=True,
            eligible=False,
            candidate_id=candidate.id,
            assessment_id=assessment.id if assessment else None,
            rights_lane=assessment.rights_lane if assessment else None,
            reason=(
                "managed_assessment_not_eligible"
                if assessment is not None
                else "managed_candidate_unassessed"
            ),
        )


def assert_clip_production_eligible(clip_id: uuid.UUID) -> ClipAcquisitionState:
    state = clip_acquisition_state(clip_id)
    if not state.eligible:
        raise ValueError(
            "clip is managed by discovery/rights qualification but has no currently "
            "production-eligible assessment"
        )
    return state
