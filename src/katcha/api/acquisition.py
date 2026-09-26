from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field

from katcha.acquisition.adapters import get_adapter
from katcha.acquisition_models import DiscoveryRun, IngestionSource
from katcha.db import session_scope
from katcha.domain import (
    AudioRightsStatus,
    DiscoveryRunStatus,
    GateStatus,
    RightsBasis,
    SourceUsageMode,
)
from katcha.orchestration.client import start_discovery_workflow
from katcha.services.acquisition import register_discovery_run
from katcha.services.ingestion_sources import (
    create_discovery_run_from_source,
    create_source_import_run,
    list_ingestion_sources,
    upsert_ingestion_source,
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


class UpsertIngestionSourceRequest(BaseModel):
    source_key: str = Field(min_length=1, max_length=128)
    name: str = Field(min_length=1, max_length=255)
    adapter_key: str = Field(min_length=1, max_length=64)
    adapter_version: str = Field(min_length=1, max_length=64)
    platform: str = Field(min_length=1, max_length=32)
    usage_mode: SourceUsageMode = SourceUsageMode.CANDIDATE_REVIEW
    channel_profile_id: uuid.UUID | None = None
    enabled: bool = True
    query_template: dict[str, object] = Field(default_factory=dict)
    default_candidate_metadata: dict[str, object] = Field(default_factory=dict)
    source_metadata: dict[str, object] = Field(default_factory=dict)
    poll_interval_minutes: int = Field(default=60, ge=1)


class IngestionSourceResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    channel_profile_id: uuid.UUID | None
    source_key: str
    name: str
    enabled: bool
    adapter_key: str
    adapter_version: str
    platform: str
    usage_mode: str
    query_template: dict[str, object]
    default_candidate_metadata: dict[str, object]
    poll_interval_minutes: int
    source_metadata: dict[str, object]
    created_at: datetime
    updated_at: datetime


class CreateSourceDiscoveryRunRequest(BaseModel):
    idempotency_key: str | None = Field(default=None, max_length=160)
    query_overrides: dict[str, object] = Field(default_factory=dict)
    metadata: dict[str, object] = Field(default_factory=dict)


class ImportSourceDropRequest(BaseModel):
    batch_key: str | None = Field(default=None, min_length=1, max_length=160)
    idempotency_key: str | None = Field(default=None, max_length=160)
    feed_key: str | None = Field(default=None, min_length=1, max_length=160)
    default_platform: str | None = Field(default=None, min_length=1, max_length=32)
    default_content_kind: str | None = Field(
        default=None,
        min_length=1,
        max_length=64,
    )
    urls: list[str] = Field(default_factory=list, max_length=500)
    items: list[dict[str, object]] = Field(default_factory=list, max_length=500)
    default_metadata: dict[str, object] = Field(default_factory=dict)
    metadata: dict[str, object] = Field(default_factory=dict)


class SourceImportRunResponse(BaseModel):
    discovery_run: DiscoveryRunResponse
    batch_key: str | None
    item_count: int


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


class DiscoveryObservationResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    discovery_run_id: uuid.UUID
    discovery_candidate_id: uuid.UUID
    external_id: str | None
    observation_metadata: dict[str, object]
    observed_at: datetime


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
    observations: list[DiscoveryObservationResponse]
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
    "/discovery/sources",
    response_model=IngestionSourceResponse,
    status_code=status.HTTP_201_CREATED,
)
def upsert_source(request: UpsertIngestionSourceRequest) -> IngestionSource:
    try:
        return upsert_ingestion_source(
            source_key=request.source_key,
            name=request.name,
            adapter_key=request.adapter_key,
            adapter_version=request.adapter_version,
            platform=request.platform,
            usage_mode=request.usage_mode,
            query_template=request.query_template,
            default_candidate_metadata=request.default_candidate_metadata,
            source_metadata=request.source_metadata,
            channel_profile_id=request.channel_profile_id,
            enabled=request.enabled,
            poll_interval_minutes=request.poll_interval_minutes,
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get(
    "/discovery/sources",
    response_model=list[IngestionSourceResponse],
)
def get_sources(
    channel_profile_id: uuid.UUID | None = Query(default=None),
    enabled: bool | None = Query(default=None),
) -> list[IngestionSource]:
    return list_ingestion_sources(
        channel_profile_id=channel_profile_id,
        enabled=enabled,
    )


@router.post(
    "/discovery/sources/{source_id}/runs",
    response_model=DiscoveryRunResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_source_run(
    source_id: uuid.UUID,
    request: CreateSourceDiscoveryRunRequest,
) -> DiscoveryRun:
    try:
        return create_discovery_run_from_source(
            source_id,
            query_overrides=request.query_overrides,
            idempotency_key=request.idempotency_key,
            metadata=request.metadata,
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post(
    "/discovery/sources/{source_id}/imports",
    response_model=SourceImportRunResponse,
    status_code=status.HTTP_201_CREATED,
)
def import_source_drop(
    source_id: uuid.UUID,
    request: ImportSourceDropRequest,
) -> SourceImportRunResponse:
    try:
        result = create_source_import_run(
            source_id,
            urls=request.urls,
            items=request.items,
            batch_key=request.batch_key,
            idempotency_key=request.idempotency_key,
            feed_key=request.feed_key,
            default_platform=request.default_platform,
            default_content_kind=request.default_content_kind,
            default_metadata=request.default_metadata,
            metadata=request.metadata,
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return SourceImportRunResponse(
        discovery_run=DiscoveryRunResponse.model_validate(result.discovery_run),
        batch_key=result.batch_key,
        item_count=result.item_count,
    )


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
