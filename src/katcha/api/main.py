from __future__ import annotations

import asyncio
import uuid

from fastapi import FastAPI, HTTPException, Query, status
from sqlalchemy import select, text

from katcha import __version__
from katcha.api.schemas import (
    AnalysisRunResponse,
    AnalyzeRequest,
    AnalyzeResponse,
    ClipFeatureResponse,
    ClipResponse,
    HealthResponse,
    IngestRequest,
    IngestResponse,
    SourceResponse,
)
from katcha.db import session_scope
from katcha.domain import SourceStatus
from katcha.models import Clip, ClipAnalysisRun, ClipFeature, SourceItem
from katcha.orchestration.client import (
    get_temporal_client,
    start_analysis_workflow,
    start_ingest_workflow,
)
from katcha.services.analysis import register_analysis
from katcha.services.sources import register_source

app = FastAPI(
    title="Katcha API",
    version=__version__,
    description="Standalone control plane for Katcha media workflows.",
)


@app.get("/v1/health/live", response_model=HealthResponse)
def live() -> HealthResponse:
    return HealthResponse(status="ok", version=__version__)


@app.get("/v1/health/ready", response_model=HealthResponse)
async def ready() -> HealthResponse:
    try:
        with session_scope() as session:
            session.execute(text("SELECT 1"))
        await asyncio.wait_for(get_temporal_client(), timeout=3)
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"dependency unavailable: {exc}") from exc
    return HealthResponse(status="ok", version=__version__)


@app.post(
    "/v1/clips/ingest",
    response_model=IngestResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def ingest(request: IngestRequest) -> IngestResponse:
    source = register_source(str(request.url), force_retry=request.force_retry)
    if source.workflow_id is None:
        raise HTTPException(status_code=500, detail="source has no workflow id")

    if source.status != SourceStatus.READY.value:
        await start_ingest_workflow(str(source.id), source.workflow_id)

    return IngestResponse(
        source_id=source.id,
        workflow_id=source.workflow_id,
        status=source.status,
        clip_id=source.clip_id,
    )


@app.post(
    "/v1/clips/{clip_id}/analyze",
    response_model=AnalyzeResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def analyze_clip(clip_id: uuid.UUID, request: AnalyzeRequest) -> AnalyzeResponse:
    try:
        run = register_analysis(clip_id, force_retry=request.force_retry)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    await start_analysis_workflow(str(run.id), run.workflow_id)
    return AnalyzeResponse(
        analysis_run_id=run.id,
        clip_id=run.clip_id,
        workflow_id=run.workflow_id,
        status=run.status,
        stage=run.stage,
    )


@app.get("/v1/analysis/{analysis_run_id}", response_model=AnalysisRunResponse)
def get_analysis_run(analysis_run_id: uuid.UUID) -> ClipAnalysisRun:
    with session_scope() as session:
        run = session.get(ClipAnalysisRun, analysis_run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="analysis run not found")
        return run


@app.get("/v1/clips/{clip_id}/features", response_model=ClipFeatureResponse)
def get_clip_features(clip_id: uuid.UUID) -> ClipFeature:
    with session_scope() as session:
        features = session.get(ClipFeature, clip_id)
        if features is None:
            raise HTTPException(status_code=404, detail="clip features not found")
        return features


@app.get("/v1/sources/{source_id}", response_model=SourceResponse)
def get_source(source_id: uuid.UUID) -> SourceItem:
    with session_scope() as session:
        source = session.get(SourceItem, source_id)
        if source is None:
            raise HTTPException(status_code=404, detail="source not found")
        return source


@app.get("/v1/sources", response_model=list[SourceResponse])
def list_sources(
    limit: int = Query(default=50, ge=1, le=250),
    source_status: str | None = Query(default=None, alias="status"),
) -> list[SourceItem]:
    with session_scope() as session:
        stmt = select(SourceItem).order_by(SourceItem.discovered_at.desc()).limit(limit)
        if source_status:
            stmt = stmt.where(SourceItem.status == source_status)
        return list(session.scalars(stmt))


@app.get("/v1/clips/{clip_id}", response_model=ClipResponse)
def get_clip(clip_id: uuid.UUID) -> Clip:
    with session_scope() as session:
        clip = session.get(Clip, clip_id)
        if clip is None:
            raise HTTPException(status_code=404, detail="clip not found")
        return clip


@app.get("/v1/clips", response_model=list[ClipResponse])
def list_clips(
    limit: int = Query(default=50, ge=1, le=250),
    clip_status: str | None = Query(default=None, alias="status"),
) -> list[Clip]:
    with session_scope() as session:
        stmt = select(Clip).order_by(Clip.created_at.desc()).limit(limit)
        if clip_status:
            stmt = stmt.where(Clip.status == clip_status)
        return list(session.scalars(stmt))
