from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import and_, func, select

from katcha.acquisition.adapters import get_adapter
from katcha.acquisition_models import (
    DiscoveryCandidate,
    DiscoveryRun,
    RightsAssessment,
    RightsEvidence,
)
from katcha.db import session_scope
from katcha.domain import (
    AudioRightsStatus,
    DiscoveryRunStatus,
    GateStatus,
    RightsBasis,
    RightsLane,
    SourceStatus,
)
from katcha.orchestration.client import (
    start_discovery_workflow,
    start_ingest_workflow,
)
from katcha.services.acquisition import (
    add_rights_evidence,
    assess_discovery_candidate,
    promote_discovery_candidate,
    register_discovery_candidate,
    register_discovery_run,
)

router = APIRouter(prefix="/v1", tags=["discovery-rights"])


class CreateDiscoveryRunRequest(BaseModel):
    adapter_key: str = Field(min_length=1, max_length=64)
    adapter_version: str = Field(min_length=1, max_length=64)
    idempotency_key: str | None = Field(default=None, max_length=160)
    query: dict[str, object] = Field(default_factory=dict)
    metadata: dict[str, object] = Field(default_factory=dict)


class DiscoveryRunResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    adapter_key: str
    adapter_version: str
    run_key: str
    status: str
    query: dict[str, object]
    cursor: dict[str, object]
    run_metadata: dict[str, object]
    error: str | None
    started_at: datetime | None
    completed_at: datetime | None
    created_at: datetime
    updated_at: datetime


class ExecuteDiscoveryRunResponse(BaseModel):
    discovery_run_id: uuid.UUID
    workflow_id: str
    status: str


class CreateDiscoveryCandidateRequest(BaseModel):
    source_url: str = Field(min_length=1, max_length=4000)
    adapter_key: str = Field(min_length=1, max_length=64)
    discovery_run_id: uuid.UUID | None = None
    external_id: str | None = Field(default=None, max_length=255)
    title: str | None = Field(default=None, max_length=2000)
    creator: str | None = Field(default=None, max_length=1000)
    creator_url: str | None = Field(default=None, max_length=4000)
    provenance_confidence: float = Field(default=0.0, ge=0, le=1)
    provenance_claims: dict[str, object] = Field(default_factory=dict)
    metadata: dict[str, object] = Field(default_factory=dict)


class DiscoveryCandidateResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    discovery_run_id: uuid.UUID | None
    source_item_id: uuid.UUID | None
    adapter_key: str
    external_id: str | None
    source_url: str
    canonical_url: str
    platform: str
    status: str
    title: str | None
    creator: str | None
    creator_url: str | None
    provenance_confidence: Decimal
    provenance_claims: dict[str, object]
    candidate_metadata: dict[str, object]
    discovered_at: datetime
    updated_at: datetime


class CreateRightsAssessmentRequest(BaseModel):
    rights_basis: RightsBasis
    audio_status: AudioRightsStatus
    originality_gate: GateStatus
    risk_flags: list[str] = Field(default_factory=list, max_length=50)
    operator_authorized: bool = False
    fair_use_factors: dict[str, object] = Field(default_factory=dict)
    metadata: dict[str, object] = Field(default_factory=dict)
    actor: str = Field(default="operator", min_length=1, max_length=128)
    reason: str | None = Field(default=None, max_length=4000)


class RightsAssessmentResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    discovery_candidate_id: uuid.UUID
    version: int
    rights_basis: str
    rights_lane: str
    rights_gate: str
    audio_status: str
    originality_gate: str
    production_eligible: bool
    operator_authorized: bool
    risk_flags: list[str]
    fair_use_factors: dict[str, object]
    assessment_metadata: dict[str, object]
    actor: str
    reason: str | None
    created_at: datetime


class CreateRightsEvidenceRequest(BaseModel):
    evidence_type: str = Field(min_length=1, max_length=64)
    source_url: str | None = Field(default=None, max_length=4000)
    snapshot_key: str | None = Field(default=None, max_length=4000)
    content_sha256: str | None = Field(default=None, max_length=64)
    terms_version: str | None = Field(default=None, max_length=128)
    metadata: dict[str, object] = Field(default_factory=dict)


class RightsEvidenceResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    rights_assessment_id: uuid.UUID
    evidence_type: str
    source_url: str | None
    snapshot_key: str | None
    content_sha256: str | None
    terms_version: str | None
    evidence_metadata: dict[str, object]
    captured_at: datetime


class CandidateSummaryResponse(BaseModel):
    candidate: DiscoveryCandidateResponse
    latest_assessment: RightsAssessmentResponse | None


class CandidateDetailResponse(BaseModel):
    candidate: DiscoveryCandidateResponse
    assessments: list[RightsAssessmentResponse]
    evidence: list[RightsEvidenceResponse]


class PromoteCandidateRequest(BaseModel):
    actor: str = Field(default="operator", min_length=1, max_length=128)


class DiscoveryPromotionResponse(BaseModel):
    discovery_candidate_id: uuid.UUID
    source_id: uuid.UUID
    workflow_id: str | None
    status: str
    clip_id: uuid.UUID | None


@router.post(
    "/discovery/runs",
    response_model=DiscoveryRunResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_discovery_run(request: CreateDiscoveryRunRequest) -> DiscoveryRun:
    return register_discovery_run(
        adapter_key=request.adapter_key,
        adapter_version=request.adapter_version,
        query=request.query,
        idempotency_key=request.idempotency_key,
        metadata=request.metadata,
    )


@router.post(
    "/discovery/runs/{run_id}/execute",
    response_model=ExecuteDiscoveryRunResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def execute_discovery_run(run_id: uuid.UUID) -> ExecuteDiscoveryRunResponse:
    with session_scope() as session:
        run = session.get(DiscoveryRun, run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="discovery run not found")
        if run.status == DiscoveryRunStatus.FAILED.value:
            raise HTTPException(
                status_code=409,
                detail="failed discovery run requires an explicit new run/idempotency key",
            )
        try:
            get_adapter(run.adapter_key, run.adapter_version)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        run_status = run.status
    workflow_id = f"discovery-run-{run_id}"
    await start_discovery_workflow(str(run_id), workflow_id)
    return ExecuteDiscoveryRunResponse(
        discovery_run_id=run_id,
        workflow_id=workflow_id,
        status=run_status,
    )


@router.post(
    "/discovery/candidates",
    response_model=DiscoveryCandidateResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_discovery_candidate(
    request: CreateDiscoveryCandidateRequest,
) -> DiscoveryCandidate:
    try:
        return register_discovery_candidate(
            source_url=request.source_url,
            adapter_key=request.adapter_key,
            discovery_run_id=request.discovery_run_id,
            external_id=request.external_id,
            title=request.title,
            creator=request.creator,
            creator_url=request.creator_url,
            provenance_confidence=request.provenance_confidence,
            provenance_claims=request.provenance_claims,
            metadata=request.metadata,
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get(
    "/discovery/candidates",
    response_model=list[CandidateSummaryResponse],
)
def list_discovery_candidates(
    candidate_status: str | None = Query(default=None, alias="status"),
    rights_lane: RightsLane | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
) -> list[CandidateSummaryResponse]:
    with session_scope() as session:
        latest_versions = (
            select(
                RightsAssessment.discovery_candidate_id.label("candidate_id"),
                func.max(RightsAssessment.version).label("version"),
            )
            .group_by(RightsAssessment.discovery_candidate_id)
            .subquery()
        )
        stmt = (
            select(DiscoveryCandidate, RightsAssessment)
            .outerjoin(
                latest_versions,
                latest_versions.c.candidate_id == DiscoveryCandidate.id,
            )
            .outerjoin(
                RightsAssessment,
                and_(
                    RightsAssessment.discovery_candidate_id == DiscoveryCandidate.id,
                    RightsAssessment.version == latest_versions.c.version,
                ),
            )
            .order_by(DiscoveryCandidate.discovered_at.desc())
            .limit(limit)
        )
        if candidate_status:
            stmt = stmt.where(DiscoveryCandidate.status == candidate_status)
        if rights_lane:
            stmt = stmt.where(RightsAssessment.rights_lane == rights_lane.value)
        rows = list(session.execute(stmt))
        return [
            CandidateSummaryResponse(
                candidate=DiscoveryCandidateResponse.model_validate(candidate),
                latest_assessment=(
                    RightsAssessmentResponse.model_validate(assessment)
                    if assessment is not None
                    else None
                ),
            )
            for candidate, assessment in rows
        ]


@router.get(
    "/discovery/candidates/{candidate_id}",
    response_model=CandidateDetailResponse,
)
def get_discovery_candidate(candidate_id: uuid.UUID) -> CandidateDetailResponse:
    with session_scope() as session:
        candidate = session.get(DiscoveryCandidate, candidate_id)
        if candidate is None:
            raise HTTPException(status_code=404, detail="discovery candidate not found")
        assessments = list(
            session.scalars(
                select(RightsAssessment)
                .where(RightsAssessment.discovery_candidate_id == candidate_id)
                .order_by(RightsAssessment.version.desc())
            )
        )
        assessment_ids = [item.id for item in assessments]
        evidence = (
            list(
                session.scalars(
                    select(RightsEvidence)
                    .where(RightsEvidence.rights_assessment_id.in_(assessment_ids))
                    .order_by(RightsEvidence.captured_at.desc())
                )
            )
            if assessment_ids
            else []
        )
        return CandidateDetailResponse(
            candidate=DiscoveryCandidateResponse.model_validate(candidate),
            assessments=[
                RightsAssessmentResponse.model_validate(item) for item in assessments
            ],
            evidence=[RightsEvidenceResponse.model_validate(item) for item in evidence],
        )


@router.post(
    "/discovery/candidates/{candidate_id}/assessments",
    response_model=RightsAssessmentResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_rights_assessment(
    candidate_id: uuid.UUID,
    request: CreateRightsAssessmentRequest,
) -> RightsAssessment:
    try:
        return assess_discovery_candidate(
            candidate_id,
            rights_basis=request.rights_basis,
            audio_status=request.audio_status,
            originality_gate=request.originality_gate,
            risk_flags=request.risk_flags,
            operator_authorized=request.operator_authorized,
            fair_use_factors=request.fair_use_factors,
            metadata=request.metadata,
            actor=request.actor,
            reason=request.reason,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post(
    "/rights/assessments/{assessment_id}/evidence",
    response_model=RightsEvidenceResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_rights_evidence(
    assessment_id: uuid.UUID,
    request: CreateRightsEvidenceRequest,
) -> RightsEvidence:
    try:
        return add_rights_evidence(
            assessment_id,
            evidence_type=request.evidence_type,
            source_url=request.source_url,
            snapshot_key=request.snapshot_key,
            content_sha256=request.content_sha256,
            terms_version=request.terms_version,
            metadata=request.metadata,
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post(
    "/discovery/candidates/{candidate_id}/promote",
    response_model=DiscoveryPromotionResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def promote_candidate(
    candidate_id: uuid.UUID,
    request: PromoteCandidateRequest,
) -> DiscoveryPromotionResponse:
    try:
        source = promote_discovery_candidate(candidate_id, actor=request.actor)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if source.status == SourceStatus.REGISTERED.value and source.workflow_id:
        await start_ingest_workflow(str(source.id), source.workflow_id)
    return DiscoveryPromotionResponse(
        discovery_candidate_id=candidate_id,
        source_id=source.id,
        workflow_id=source.workflow_id,
        status=source.status,
        clip_id=source.clip_id,
    )
