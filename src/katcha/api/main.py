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
    CreateProductionRequest,
    HealthResponse,
    IngestRequest,
    IngestResponse,
    ProductionAssetResponse,
    ProductionDetailResponse,
    ProductionResponse,
    ProductionReviewResponse,
    ProductionScriptResponse,
    ReviewActionResponse,
    ReviewProductionRequest,
    SourceResponse,
)
from katcha.config import get_settings
from katcha.db import session_scope
from katcha.domain import AnalysisStatus, ProductionStatus, ReviewDecision, SourceStatus
from katcha.models import Clip, ClipAnalysisRun, ClipFeature, SourceItem
from katcha.orchestration.client import (
    get_temporal_client,
    start_analysis_workflow,
    start_ingest_workflow,
    start_production_workflow,
)
from katcha.production_models import (
    Production,
    ProductionAsset,
    ProductionReview,
    ProductionScript,
)
from katcha.services.analysis import register_analysis
from katcha.services.productions import (
    register_regeneration,
    register_short_production,
    review_production,
)
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

    if source.status == SourceStatus.REGISTERED.value:
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
    if run.status == AnalysisStatus.QUEUED.value:
        await start_analysis_workflow(str(run.id), run.workflow_id)
    return AnalyzeResponse(
        analysis_run_id=run.id,
        clip_id=run.clip_id,
        workflow_id=run.workflow_id,
        status=run.status,
        stage=run.stage,
    )


@app.post(
    "/v1/clips/{clip_id}/productions",
    response_model=ProductionResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def create_production(
    clip_id: uuid.UUID,
    request: CreateProductionRequest,
) -> Production:
    settings = get_settings()
    if not settings.ai_enabled:
        raise HTTPException(status_code=503, detail="AI execution is disabled")
    if not settings.openai_api_key and not settings.gemini_api_key:
        raise HTTPException(status_code=503, detail="no AI/TTS provider key is configured")
    try:
        production = register_short_production(
            clip_id,
            persona_key=request.persona_key,
            idempotency_key=request.idempotency_key,
        )
    except ValueError as exc:
        code = 404 if "not found" in str(exc) else 409
        raise HTTPException(status_code=code, detail=str(exc)) from exc
    if production.status == ProductionStatus.QUEUED.value:
        await start_production_workflow(str(production.id), production.workflow_id)
    return production


@app.get("/v1/productions", response_model=list[ProductionResponse])
def list_productions(
    limit: int = Query(default=50, ge=1, le=250),
    production_status: str | None = Query(default=None, alias="status"),
) -> list[Production]:
    with session_scope() as session:
        stmt = select(Production).order_by(Production.created_at.desc()).limit(limit)
        if production_status:
            stmt = stmt.where(Production.status == production_status)
        return list(session.scalars(stmt))


@app.get("/v1/productions/{production_id}", response_model=ProductionDetailResponse)
def get_production(production_id: uuid.UUID) -> ProductionDetailResponse:
    with session_scope() as session:
        production = session.get(Production, production_id)
        if production is None:
            raise HTTPException(status_code=404, detail="production not found")
        scripts = list(
            session.scalars(
                select(ProductionScript)
                .where(ProductionScript.production_id == production_id)
                .order_by(ProductionScript.candidate_index)
            )
        )
        assets = list(
            session.scalars(
                select(ProductionAsset)
                .where(ProductionAsset.production_id == production_id)
                .order_by(ProductionAsset.created_at)
            )
        )
        reviews = list(
            session.scalars(
                select(ProductionReview)
                .where(ProductionReview.production_id == production_id)
                .order_by(ProductionReview.created_at)
            )
        )
        return ProductionDetailResponse(
            production=ProductionResponse.model_validate(production),
            scripts=[ProductionScriptResponse.model_validate(item) for item in scripts],
            assets=[ProductionAssetResponse.model_validate(item) for item in assets],
            reviews=[ProductionReviewResponse.model_validate(item) for item in reviews],
        )


@app.post(
    "/v1/productions/{production_id}/review",
    response_model=ReviewActionResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def review_short(
    production_id: uuid.UUID,
    request: ReviewProductionRequest,
) -> ReviewActionResponse:
    if request.decision == ReviewDecision.REGENERATE.value:
        try:
            child = register_regeneration(
                production_id,
                stage=request.regenerate_from,
                note=request.note,
                actor=request.actor,
            )
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        await start_production_workflow(
            str(child.id),
            child.workflow_id,
            start_stage=request.regenerate_from,
        )
        return ReviewActionResponse(
            production_id=production_id,
            decision=request.decision,
            child_production_id=child.id,
            child_workflow_id=child.workflow_id,
        )

    try:
        review_production(
            production_id,
            decision=ReviewDecision(request.decision),
            note=request.note,
            actor=request.actor,
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return ReviewActionResponse(
        production_id=production_id,
        decision=request.decision,
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
