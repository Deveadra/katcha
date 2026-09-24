from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict

from katcha.orchestration.client import start_reach_sync_workflow
from katcha.reach_models import (
    PublicationReachObservation,
    YouTubeReachReportImport,
    YouTubeReachReportingJob,
)
from katcha.services.reach_reporting import (
    ensure_local_reach_job,
    reach_imports_for_connection,
    reach_observations_for_publication,
    reach_summary_for_publication,
)

router = APIRouter(tags=["reach"])


class ReachSyncResponse(BaseModel):
    connection_id: uuid.UUID
    reach_job_id: uuid.UUID
    workflow_id: str


class ReachJobResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    youtube_connection_id: uuid.UUID
    report_type_id: str
    provider_job_id: str | None
    status: str
    stage: str
    error: str | None
    job_metadata: dict[str, object]
    created_at: datetime
    updated_at: datetime


class ReachObservationResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    publication_id: uuid.UUID
    report_import_id: uuid.UUID
    report_date: date
    impressions: int | None
    ctr: Decimal | None
    attribution_status: str
    packaging_variant_id: uuid.UUID | None
    attribution_metadata: dict[str, object]
    raw_row: dict[str, object]
    created_at: datetime



class ReachImportResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    youtube_connection_id: uuid.UUID
    reach_job_id: uuid.UUID
    provider_report_id: str
    report_start: datetime | None
    report_end: datetime | None
    provider_created_at: datetime | None
    payload_sha256: str
    row_count: int
    import_metadata: dict[str, object]
    imported_at: datetime


class ReachSummaryResponse(BaseModel):
    publication_id: str
    maturity_days: int
    report_days: int
    mixed_days: int
    legacy_days: int
    variants: list[dict[str, object]]


@router.post(
    "/v1/integrations/youtube/{connection_id}/reach/sync",
    response_model=ReachSyncResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def sync_reach(connection_id: uuid.UUID) -> ReachSyncResponse:
    try:
        reach_job = ensure_local_reach_job(connection_id)
    except ValueError as exc:
        code = 404 if "not found" in str(exc) else 409
        raise HTTPException(status_code=code, detail=str(exc)) from exc
    workflow_id = f"yt-reach-sync-{connection_id}-{uuid.uuid4().hex[:16]}"
    await start_reach_sync_workflow(str(connection_id), workflow_id)
    return ReachSyncResponse(
        connection_id=connection_id,
        reach_job_id=reach_job.id,
        workflow_id=workflow_id,
    )


@router.get(
    "/v1/integrations/youtube/{connection_id}/reach/job",
    response_model=ReachJobResponse,
)
def get_reach_job(connection_id: uuid.UUID) -> YouTubeReachReportingJob:
    try:
        return ensure_local_reach_job(connection_id)
    except ValueError as exc:
        code = 404 if "not found" in str(exc) else 409
        raise HTTPException(status_code=code, detail=str(exc)) from exc


@router.get(
    "/v1/publications/{publication_id}/reach",
    response_model=list[ReachObservationResponse],
)
def get_publication_reach(
    publication_id: uuid.UUID,
) -> list[PublicationReachObservation]:
    try:
        return reach_observations_for_publication(publication_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc



@router.get(
    "/v1/integrations/youtube/{connection_id}/reach/imports",
    response_model=list[ReachImportResponse],
)
def get_reach_imports(
    connection_id: uuid.UUID,
) -> list[YouTubeReachReportImport]:
    try:
        return reach_imports_for_connection(connection_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get(
    "/v1/publications/{publication_id}/reach/summary",
    response_model=ReachSummaryResponse,
)
def get_publication_reach_summary(
    publication_id: uuid.UUID,
    maturity_days: int = Query(default=7, ge=1, le=90),
) -> dict[str, object]:
    try:
        return reach_summary_for_publication(
            publication_id,
            maturity_days=maturity_days,
        )
    except ValueError as exc:
        code = 404 if "not found" in str(exc) else 409
        raise HTTPException(status_code=code, detail=str(exc)) from exc
