from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from fastapi import APIRouter, HTTPException, Query, UploadFile, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import and_, func, select

from katcha.acquisition.adapters import available_adapters, get_adapter
from katcha.acquisition_models import (
    DiscoveryCandidate,
    DiscoveryObservation,
    DiscoveryRun,
    IngestionSource,
    IntelligenceIngestBatch,
    IntelligenceRecord,
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
    SourceUsageMode,
)
from katcha.intelligence_ingest_contract import IngestIntelligenceBatchRequest
from katcha.orchestration.client import (
    start_discovery_workflow,
    start_ingest_workflow,
)
from katcha.services.acquisition import (
    add_rights_evidence,
    assess_discovery_candidate,
    promote_discovery_candidate,
    register_discovery_run,
)
from katcha.services.discovery import observe_discovery_candidate
from katcha.services.ingestion_sources import (
    create_discovery_run_from_source,
    create_source_import_run,
    get_ingestion_source_overview,
    get_intelligence_record,
    ingest_intelligence_batch,
    list_ingestion_source_library,
    list_ingestion_sources,
    list_intelligence_records,
    list_source_finds,
    list_source_runs,
    restart_source_run,
    upsert_ingestion_source,
)
from katcha.services.intelligence_handoff import (
    HandoffInboxItem,
    handoff_inbox_summary,
    list_handoff_inbox,
    process_handoff_file,
    process_handoff_inbox,
    submit_handoff_file,
)

router = APIRouter(prefix="/v1", tags=["discovery-rights"])


class CreateDiscoveryRunRequest(BaseModel):
    adapter_key: str = Field(min_length=1, max_length=64)
    adapter_version: str = Field(min_length=1, max_length=64)
    idempotency_key: str | None = Field(default=None, max_length=160)
    query: dict[str, object] = Field(default_factory=dict)
    metadata: dict[str, object] = Field(default_factory=dict)


class DiscoveryAdapterResponse(BaseModel):
    key: str
    version: str
    label: str
    description: str
    source_types: list[str]
    supported_platforms: list[str]
    query_fields: list[str]
    required_credentials: list[str]
    supports_imports: bool
    sample_query: dict[str, object]


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
    create_only: bool = False
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


class SourceLibraryPageResponse(BaseModel):
    total: int
    limit: int
    offset: int
    items: list[IngestionSourceResponse]


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


class IntelligenceRecordResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    channel_profile_id: uuid.UUID
    first_batch_id: uuid.UUID
    last_batch_id: uuid.UUID
    record_kind: str
    record_key: str
    title: str | None
    summary: str | None
    source_url: str | None
    platform: str | None
    status: str
    tags: list[str]
    payload: dict[str, object]
    provenance: dict[str, object]
    observed_at: datetime
    event_time: datetime | None
    created_at: datetime
    updated_at: datetime


class IngestIntelligenceBatchResponse(BaseModel):
    batch_id: uuid.UUID
    channel_profile_id: uuid.UUID
    batch_key: str
    producer: str
    source_type: str
    content_sha256: str
    record_count: int
    created_count: int
    updated_count: int
    replayed: bool
    records: list[IntelligenceRecordResponse]


class MaterializeIntelligenceCandidateRequest(BaseModel):
    adapter_key: str = Field(default="operator_feed", min_length=1, max_length=64)
    provenance_confidence: float | None = Field(default=None, ge=0, le=1)
    honor_operator_authorization: bool = True


class HandoffInboxItemResponse(BaseModel):
    filename: str
    status: str
    size_bytes: int
    modified_at: datetime
    channel_profile_id: str | None = None
    batch_key: str | None = None
    record_count: int | None = None
    error: str | None = None
    receipt: dict[str, object] | None = None


class HandoffInboxResponse(BaseModel):
    incoming_path: str
    max_file_bytes: int
    counts: dict[str, int]
    items: list[HandoffInboxItemResponse]


class ProcessHandoffInboxRequest(BaseModel):
    filenames: list[str] = Field(default_factory=list, max_length=100)
    limit: int = Field(default=50, ge=1, le=500)


class ExecuteDiscoveryRunResponse(BaseModel):
    discovery_run_id: uuid.UUID
    workflow_id: str
    status: str


class RestartDiscoveryRunRequest(BaseModel):
    idempotency_key: str | None = Field(default=None, max_length=160)


class RestartDiscoveryRunResponse(BaseModel):
    previous_run_id: uuid.UUID
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


class SourceRecentFindResponse(BaseModel):
    candidate: DiscoveryCandidateResponse
    observed_at: datetime


class SourceFindPageResponse(BaseModel):
    total: int
    limit: int
    offset: int
    items: list[SourceRecentFindResponse]


class SourceOverviewResponse(BaseModel):
    source: IngestionSourceResponse
    channel_name: str | None
    channel_status: str | None
    run_count: int
    completed_runs: int
    failed_runs: int
    running_runs: int
    queued_runs: int
    success_rate: float | None
    discovery_count: int
    unique_candidate_count: int
    recent_runs: list[DiscoveryRunResponse]
    recent_finds: list[SourceRecentFindResponse]


class SourceRunResultsResponse(BaseModel):
    total: int
    candidates: list[DiscoveryCandidateResponse]


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
    for_review: bool = False
    actor: str = Field(default="operator", min_length=1, max_length=128)


class DiscoveryPromotionResponse(BaseModel):
    discovery_candidate_id: uuid.UUID
    source_id: uuid.UUID
    workflow_id: str | None
    status: str
    clip_id: uuid.UUID | None


@router.get(
    "/discovery/adapters",
    response_model=list[DiscoveryAdapterResponse],
)
def list_discovery_adapters() -> list[dict[str, object]]:
    return available_adapters()


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
            create_only=request.create_only,
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


@router.get(
    "/discovery/source-library",
    response_model=SourceLibraryPageResponse,
)
def get_source_library(
    q: str | None = Query(default=None, max_length=200),
    channel_profile_id: uuid.UUID | None = Query(default=None),
    shared_only: bool = Query(default=False),
    platform: str | None = Query(default=None, max_length=32),
    adapter_key: str | None = Query(default=None, max_length=64),
    usage_mode: str | None = Query(default=None, max_length=32),
    enabled: bool | None = Query(default=None),
    sort: str = Query(default="recent", pattern="^(recent|name|created)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> SourceLibraryPageResponse:
    try:
        result = list_ingestion_source_library(
            query=q,
            channel_profile_id=channel_profile_id,
            shared_only=shared_only,
            platform=platform,
            adapter_key=adapter_key,
            usage_mode=usage_mode,
            enabled=enabled,
            sort=sort,
            limit=limit,
            offset=offset,
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return SourceLibraryPageResponse(
        total=result.total,
        limit=result.limit,
        offset=result.offset,
        items=[
            IngestionSourceResponse.model_validate(row)
            for row in result.items
        ],
    )


@router.get(
    "/discovery/sources/{source_id}/finds",
    response_model=SourceFindPageResponse,
)
def get_source_finds(
    source_id: uuid.UUID,
    q: str | None = Query(default=None, max_length=200),
    candidate_status: str | None = Query(default=None, alias="status", max_length=32),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> SourceFindPageResponse:
    try:
        page = list_source_finds(
            source_id,
            query=q,
            status=candidate_status,
            limit=limit,
            offset=offset,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return SourceFindPageResponse(
        total=page.total,
        limit=page.limit,
        offset=page.offset,
        items=[
            SourceRecentFindResponse(
                candidate=DiscoveryCandidateResponse.model_validate(item.candidate),
                observed_at=item.observed_at,
            )
            for item in page.items
        ],
    )


@router.get(
    "/discovery/sources/{source_id}/overview",
    response_model=SourceOverviewResponse,
)
def get_source_overview(source_id: uuid.UUID) -> SourceOverviewResponse:
    try:
        overview = get_ingestion_source_overview(source_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    completed = int(overview.status_counts.get(DiscoveryRunStatus.COMPLETED.value, 0))
    failed = int(overview.status_counts.get(DiscoveryRunStatus.FAILED.value, 0))
    terminal = completed + failed
    success_rate = (completed / terminal) if terminal else None
    return SourceOverviewResponse(
        source=IngestionSourceResponse.model_validate(overview.source),
        channel_name=overview.channel_name,
        channel_status=overview.channel_status,
        run_count=overview.run_count,
        completed_runs=completed,
        failed_runs=failed,
        running_runs=int(
            overview.status_counts.get(DiscoveryRunStatus.RUNNING.value, 0)
        ),
        queued_runs=int(
            overview.status_counts.get(DiscoveryRunStatus.QUEUED.value, 0)
        ),
        success_rate=success_rate,
        discovery_count=overview.discovery_count,
        unique_candidate_count=overview.unique_candidate_count,
        recent_runs=[
            DiscoveryRunResponse.model_validate(row)
            for row in overview.recent_runs
        ],
        recent_finds=[
            SourceRecentFindResponse(
                candidate=DiscoveryCandidateResponse.model_validate(item.candidate),
                observed_at=item.observed_at,
            )
            for item in overview.recent_finds
        ],
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
    "/intelligence-ingest/batches",
    response_model=IngestIntelligenceBatchResponse,
)
def create_intelligence_ingest_batch(
    request: IngestIntelligenceBatchRequest,
) -> IngestIntelligenceBatchResponse:
    try:
        result = ingest_intelligence_batch(
            channel_profile_id=request.channel_profile_id,
            batch_key=request.batch_key,
            producer=request.producer,
            source_type=request.source_type,
            batch_metadata=request.batch_metadata,
            records=[item.model_dump(mode="python") for item in request.records],
        )
    except ValueError as exc:
        code = 404 if "channel profile not found" in str(exc) else 409
        raise HTTPException(status_code=code, detail=str(exc)) from exc
    batch: IntelligenceIngestBatch = result.batch
    return IngestIntelligenceBatchResponse(
        batch_id=batch.id,
        channel_profile_id=batch.channel_profile_id,
        batch_key=batch.batch_key,
        producer=batch.producer,
        source_type=batch.source_type,
        content_sha256=batch.content_sha256,
        record_count=batch.record_count,
        created_count=result.created_count,
        updated_count=result.updated_count,
        replayed=result.replayed,
        records=[
            IntelligenceRecordResponse.model_validate(row)
            for row in result.records
        ],
    )


def _handoff_response(item: HandoffInboxItem) -> HandoffInboxItemResponse:
    return HandoffInboxItemResponse(
        filename=item.filename,
        status=item.status,
        size_bytes=item.size_bytes,
        modified_at=item.modified_at,
        channel_profile_id=item.channel_profile_id,
        batch_key=item.batch_key,
        record_count=item.record_count,
        error=item.error,
        receipt=item.receipt,
    )


@router.get(
    "/intelligence-ingest/inbox",
    response_model=HandoffInboxResponse,
)
def get_intelligence_handoff_inbox(
    limit: int = Query(default=100, ge=1, le=500),
) -> HandoffInboxResponse:
    try:
        summary = handoff_inbox_summary()
        items = list_handoff_inbox(limit=limit)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return HandoffInboxResponse(
        incoming_path="handoff/incoming",
        max_file_bytes=int(summary["max_file_bytes"]),
        counts={str(key): int(value) for key, value in summary["counts"].items()},
        items=[_handoff_response(item) for item in items],
    )


@router.post(
    "/intelligence-ingest/inbox/files",
    response_model=HandoffInboxItemResponse,
    status_code=status.HTTP_201_CREATED,
)
async def upload_intelligence_handoff_file(
    file: UploadFile,
    process: bool = Query(default=True),
) -> HandoffInboxItemResponse:
    filename = str(file.filename or "").strip()
    content = await file.read(10 * 1024 * 1024 + 1)
    try:
        item = submit_handoff_file(filename, content)
        if process and item.status == "incoming":
            item = process_handoff_file(item.filename)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _handoff_response(item)


@router.post(
    "/intelligence-ingest/inbox/process",
    response_model=list[HandoffInboxItemResponse],
)
def process_intelligence_handoff_files(
    request: ProcessHandoffInboxRequest,
) -> list[HandoffInboxItemResponse]:
    try:
        if request.filenames:
            items = [
                process_handoff_file(filename)
                for filename in dict.fromkeys(request.filenames)
            ]
        else:
            items = process_handoff_inbox(limit=request.limit)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return [_handoff_response(item) for item in items]


@router.get(
    "/channels/{channel_profile_id}/intelligence-records",
    response_model=list[IntelligenceRecordResponse],
)
def get_channel_intelligence_records(
    channel_profile_id: uuid.UUID,
    record_kind: str | None = Query(default=None, max_length=64),
    record_status: str | None = Query(default=None, alias="status", max_length=32),
    limit: int = Query(default=100, ge=1, le=500),
) -> list[IntelligenceRecord]:
    try:
        return list_intelligence_records(
            channel_profile_id,
            record_kind=record_kind,
            record_status=record_status,
            limit=limit,
        )
    except ValueError as exc:
        code = 404 if "channel profile not found" in str(exc) else 409
        raise HTTPException(status_code=code, detail=str(exc)) from exc


@router.get(
    "/channels/{channel_profile_id}/intelligence-records/{record_id}",
    response_model=IntelligenceRecordResponse,
)
def get_channel_intelligence_record(
    channel_profile_id: uuid.UUID,
    record_id: uuid.UUID,
) -> IntelligenceRecord:
    try:
        return get_intelligence_record(channel_profile_id, record_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post(
    "/channels/{channel_profile_id}/intelligence-records/{record_id}/discovery-candidate",
    response_model=DiscoveryCandidateResponse,
    status_code=status.HTTP_201_CREATED,
)
def materialize_intelligence_candidate(
    channel_profile_id: uuid.UUID,
    record_id: uuid.UUID,
    request: MaterializeIntelligenceCandidateRequest,
) -> DiscoveryCandidate:
    try:
        record = get_intelligence_record(channel_profile_id, record_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if not record.source_url:
        raise HTTPException(
            status_code=409,
            detail="intelligence record has no source_url to materialize",
        )

    provenance = dict(record.provenance or {})
    confidence = request.provenance_confidence
    if confidence is None:
        try:
            confidence = float(provenance.get("confidence", 0.0) or 0.0)
        except (TypeError, ValueError):
            confidence = 0.0
    confidence = max(0.0, min(float(confidence), 1.0))
    payload = dict(record.payload or {})
    creator = str(payload.get("creator") or payload.get("channel_name") or "").strip() or None
    creator_url = str(payload.get("creator_url") or "").strip() or None

    authorization_scope = str(payload.get("authorization_scope") or "").strip()
    official_source_verified = bool(
        provenance.get("official_channel_verified")
        or payload.get("official_source_verified")
    )
    standing_authorized = (
        request.honor_operator_authorization
        and bool(payload.get("operator_authorized"))
        and official_source_verified
        and authorization_scope == "official_trailer_repost"
    )

    try:
        candidate = observe_discovery_candidate(
            source_url=record.source_url,
            adapter_key=request.adapter_key,
            external_id=record.record_key,
            title=record.title,
            creator=creator,
            creator_url=creator_url,
            provenance_confidence=confidence,
            provenance_claims={
                **provenance,
                "intelligence_record_id": str(record.id),
                "intelligence_record_kind": record.record_kind,
            },
            metadata={
                "channel_profile_id": str(channel_profile_id),
                "intelligence_record_id": str(record.id),
                "intelligence_record_kind": record.record_kind,
                "intelligence_title": record.title,
                "intelligence_summary": record.summary,
                "intelligence_tags": list(record.tags or []),
                "intelligence_payload": payload,
                "source_type": "intelligence_handoff",
                "operator_authorized": standing_authorized,
                "authorization_scope": authorization_scope or None,
                "official_source_verified": official_source_verified,
            },
        )
        if standing_authorized:
            assess_discovery_candidate(
                candidate.id,
                rights_basis=RightsBasis.OPERATOR_AUTHORIZED,
                audio_status=AudioRightsStatus.ORIGINAL,
                originality_gate=GateStatus.CLEARED,
                risk_flags=[],
                operator_authorized=True,
                metadata={
                    "authorization_source": "intelligence_handoff",
                    "authorization_scope": authorization_scope,
                    "official_source_verified": True,
                    "intelligence_record_id": str(record.id),
                },
                actor="operator",
                reason=(
                    "Standing operator authorization for verified official-trailer "
                    "republication"
                ),
            )
            with session_scope() as session:
                refreshed = session.get(DiscoveryCandidate, candidate.id)
                if refreshed is None:
                    raise RuntimeError("materialized discovery candidate disappeared")
                session.expunge(refreshed)
                candidate = refreshed
        return candidate
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


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
    "/discovery/runs/{run_id}/restart",
    response_model=RestartDiscoveryRunResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def restart_failed_discovery_run(
    run_id: uuid.UUID,
    request: RestartDiscoveryRunRequest,
) -> RestartDiscoveryRunResponse:
    try:
        run = restart_source_run(
            run_id,
            idempotency_key=request.idempotency_key,
        )
    except ValueError as exc:
        message = str(exc)
        code = 404 if "not found" in message else 409
        raise HTTPException(status_code=code, detail=message) from exc

    workflow_id = f"discovery-run-{run.id}"
    await start_discovery_workflow(str(run.id), workflow_id)
    return RestartDiscoveryRunResponse(
        previous_run_id=run_id,
        discovery_run_id=run.id,
        workflow_id=workflow_id,
        status=run.status,
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
        return observe_discovery_candidate(
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
        observations = list(
            session.scalars(
                select(DiscoveryObservation)
                .where(DiscoveryObservation.discovery_candidate_id == candidate_id)
                .order_by(DiscoveryObservation.observed_at.desc())
            )
        )
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
            observations=[
                DiscoveryObservationResponse.model_validate(item)
                for item in observations
            ],
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
        source = promote_discovery_candidate(
            candidate_id, actor=request.actor, for_review=request.for_review,
        )
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


@router.get("/discovery/sources/{source_id}/runs", response_model=list[DiscoveryRunResponse])
def source_run_history(source_id: uuid.UUID, limit: int = Query(default=50, ge=1, le=100)):
    try:
        return list_source_runs(source_id, limit=limit)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get(
    "/discovery/sources/{source_id}/runs/{run_id}/results",
    response_model=SourceRunResultsResponse,
)
def source_run_results(source_id: uuid.UUID, run_id: uuid.UUID) -> SourceRunResultsResponse:
    """Summarize one source's run without exposing another source's candidates."""
    with session_scope() as session:
        run = session.get(DiscoveryRun, run_id)
        if (
            session.get(IngestionSource, source_id) is None
            or run is None
            or (run.run_metadata or {}).get("ingestion_source_id") != str(source_id)
        ):
            raise HTTPException(status_code=404, detail="source run not found")
        condition = DiscoveryObservation.discovery_run_id == run_id
        total = session.scalar(
            select(func.count()).select_from(DiscoveryObservation).where(condition)
        ) or 0
        rows = session.scalars(
            select(DiscoveryCandidate)
            .join(
                DiscoveryObservation,
                DiscoveryObservation.discovery_candidate_id == DiscoveryCandidate.id,
            )
            .where(condition)
            .order_by(DiscoveryObservation.observed_at.desc(), DiscoveryObservation.id.desc())
            .limit(5)
        )
        return SourceRunResultsResponse(
            total=total,
            candidates=[DiscoveryCandidateResponse.model_validate(row) for row in rows],
        )
