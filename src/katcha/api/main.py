from __future__ import annotations

import asyncio
import uuid
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Query, status
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select, text

from katcha import __version__
from katcha.api.acquisition import router as acquisition_router
from katcha.api.control_auth import require_control_token
from katcha.api.edit_blueprints import router as edit_blueprints_router
from katcha.api.explorer import router as explorer_router
from katcha.api.intelligence import router as intelligence_router
from katcha.api.schemas import (
    AnalysisRunResponse,
    AnalyticsRefreshResponse,
    AnalyticsSnapshotDetailResponse,
    AnalyzeRequest,
    AnalyzeResponse,
    ClipFeatureResponse,
    ClipResponse,
    CompilationAssetResponse,
    CompilationDetailResponse,
    CompilationResponse,
    CompilationReviewResponse,
    CompilationSegmentResponse,
    CreateCompilationRequest,
    CreateProductionRequest,
    CreatePublicationRequest,
    HealthResponse,
    IngestRequest,
    IngestResponse,
    ProductionAssetResponse,
    ProductionDetailResponse,
    ProductionResponse,
    ProductionReviewResponse,
    ProductionScriptResponse,
    PublicationAnalyticsSnapshotResponse,
    PublicationResponse,
    RetentionPointResponse,
    RetryPublicationRequest,
    ReviewActionResponse,
    ReviewCompilationRequest,
    ReviewCompilationResponse,
    ReviewProductionRequest,
    SourceResponse,
    YouTubeConnectionResponse,
    YouTubeOAuthStartResponse,
)
from katcha.api.short_episodes import router as short_episodes_router
from katcha.api.trends import router as trends_router
from katcha.config import get_settings
from katcha.db import session_scope
from katcha.domain import (
    AnalysisStatus,
    CompilationStatus,
    ProductionStatus,
    ReviewDecision,
    SourceStatus,
)
from katcha.integrations.youtube.oauth import (
    YouTubeOAuthError,
    begin_youtube_oauth,
    complete_youtube_oauth,
)
from katcha.longform_models import (
    Compilation,
    CompilationAsset,
    CompilationReview,
    CompilationSegment,
)
from katcha.models import Clip, ClipAnalysisRun, ClipFeature, SourceItem
from katcha.orchestration.client import (
    get_temporal_client,
    start_analysis_workflow,
    start_analytics_refresh_workflow,
    start_ingest_workflow,
    start_longform_workflow,
    start_production_workflow,
    start_publication_workflow,
)
from katcha.production_models import (
    Production,
    ProductionAsset,
    ProductionReview,
    ProductionScript,
)
from katcha.publishing_models import (
    Publication,
    PublicationAnalyticsSnapshot,
    RetentionPoint,
    YouTubeConnection,
)
from katcha.services.analysis import register_analysis
from katcha.services.compilations import (
    register_compilation,
    register_compilation_regeneration,
    review_compilation,
)
from katcha.services.productions import (
    register_regeneration,
    register_short_production,
    review_production,
)
from katcha.services.publications import (
    analytics_refresh_workflow_id,
    register_compilation_publication,
    register_publication,
    retry_publication,
)
from katcha.services.sources import register_source

app = FastAPI(
    title="Katcha API",
    dependencies=[Depends(require_control_token)],
    version=__version__,
    description="Standalone control plane for Katcha media workflows.",
)
app.include_router(acquisition_router)
app.include_router(edit_blueprints_router)
app.include_router(intelligence_router)
app.include_router(short_episodes_router)
app.include_router(trends_router)
app.include_router(explorer_router)
app.mount("/explorer/assets", StaticFiles(directory=Path(__file__).parents[1] / "web"),
          name="explorer-assets")


@app.get("/explorer", include_in_schema=False)
def explorer_shell():
    return RedirectResponse("/explorer/assets/index.html")


def _require_ai_execution() -> None:
    settings = get_settings()
    if not settings.ai_enabled:
        raise HTTPException(status_code=503, detail="AI execution is disabled")
    if not settings.openai_api_key and not settings.gemini_api_key:
        raise HTTPException(status_code=503, detail="no AI/TTS provider key is configured")


def _require_youtube_execution() -> None:
    settings = get_settings()
    if not settings.youtube_client_id or not settings.youtube_client_secret:
        raise HTTPException(status_code=503, detail="YouTube OAuth client is not configured")
    if not settings.credential_encryption_key:
        raise HTTPException(status_code=503, detail="credential encryption is not configured")


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


@app.get(
    "/v1/integrations/youtube/oauth/start",
    response_model=YouTubeOAuthStartResponse,
)
def youtube_oauth_start() -> YouTubeOAuthStartResponse:
    try:
        return YouTubeOAuthStartResponse(authorization_url=begin_youtube_oauth())
    except (YouTubeOAuthError, RuntimeError, ValueError) as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.get(
    "/v1/integrations/youtube/oauth/callback",
    response_model=YouTubeConnectionResponse,
)
async def youtube_oauth_callback(
    state: str,
    code: str | None = None,
    error: str | None = None,
) -> YouTubeConnection:
    if error:
        raise HTTPException(status_code=400, detail=f"Google OAuth failed: {error}")
    if not code:
        raise HTTPException(status_code=400, detail="Google OAuth callback did not include a code")
    try:
        return await asyncio.to_thread(complete_youtube_oauth, state=state, code=code)
    except (YouTubeOAuthError, RuntimeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get(
    "/v1/integrations/youtube",
    response_model=list[YouTubeConnectionResponse],
)
def list_youtube_connections() -> list[YouTubeConnection]:
    with session_scope() as session:
        stmt = select(YouTubeConnection).order_by(YouTubeConnection.created_at)
        return list(session.scalars(stmt))


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
    _require_ai_execution()
    try:
        production = register_short_production(
            clip_id,
            persona_key=request.persona_key,
            idempotency_key=request.idempotency_key,
            edit_blueprint_key=request.edit_blueprint_key,
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


@app.post(
    "/v1/productions/{production_id}/publications",
    response_model=PublicationResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def create_publication(
    production_id: uuid.UUID,
    request: CreatePublicationRequest,
) -> Publication:
    _require_youtube_execution()
    try:
        publication = register_publication(
            production_id,
            youtube_connection_id=request.youtube_connection_id,
            title=request.title,
            description=request.description,
            tags=request.tags,
            category_id=request.category_id,
            privacy_status=request.privacy_status,
            publish_at=request.publish_at,
            notify_subscribers=request.notify_subscribers,
            made_for_kids=request.made_for_kids,
            contains_synthetic_media=request.contains_synthetic_media,
        )
    except ValueError as exc:
        code = 404 if "not found" in str(exc) else 409
        raise HTTPException(status_code=code, detail=str(exc)) from exc
    if publication.status == "queued":
        await start_publication_workflow(str(publication.id), publication.workflow_id)
    return publication


@app.post(
    "/v1/compilations",
    response_model=CompilationResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def create_compilation(request: CreateCompilationRequest) -> Compilation:
    _require_ai_execution()
    try:
        compilation = register_compilation(
            theme=request.theme,
            target_duration_seconds=request.target_duration_seconds,
            target_segment_count=request.target_segment_count,
            persona_key=request.persona_key,
            idempotency_key=request.idempotency_key,
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if compilation.status == CompilationStatus.QUEUED.value:
        await start_longform_workflow(
            str(compilation.id),
            compilation.workflow_id,
            start_stage="select",
        )
    return compilation


@app.get("/v1/compilations", response_model=list[CompilationResponse])
def list_compilations(
    limit: int = Query(default=50, ge=1, le=250),
    compilation_status: str | None = Query(default=None, alias="status"),
) -> list[Compilation]:
    with session_scope() as session:
        stmt = select(Compilation).order_by(Compilation.created_at.desc()).limit(limit)
        if compilation_status:
            stmt = stmt.where(Compilation.status == compilation_status)
        return list(session.scalars(stmt))


@app.get("/v1/compilations/{compilation_id}", response_model=CompilationDetailResponse)
def get_compilation(compilation_id: uuid.UUID) -> CompilationDetailResponse:
    with session_scope() as session:
        compilation = session.get(Compilation, compilation_id)
        if compilation is None:
            raise HTTPException(status_code=404, detail="compilation not found")
        segments = list(
            session.scalars(
                select(CompilationSegment)
                .where(CompilationSegment.compilation_id == compilation_id)
                .order_by(CompilationSegment.position)
            )
        )
        assets = list(
            session.scalars(
                select(CompilationAsset)
                .where(CompilationAsset.compilation_id == compilation_id)
                .order_by(CompilationAsset.created_at)
            )
        )
        reviews = list(
            session.scalars(
                select(CompilationReview)
                .where(CompilationReview.compilation_id == compilation_id)
                .order_by(CompilationReview.created_at)
            )
        )
        return CompilationDetailResponse(
            compilation=CompilationResponse.model_validate(compilation),
            segments=[CompilationSegmentResponse.model_validate(item) for item in segments],
            assets=[CompilationAssetResponse.model_validate(item) for item in assets],
            reviews=[CompilationReviewResponse.model_validate(item) for item in reviews],
        )


@app.post(
    "/v1/compilations/{compilation_id}/review",
    response_model=ReviewCompilationResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def review_longform(
    compilation_id: uuid.UUID,
    request: ReviewCompilationRequest,
) -> ReviewCompilationResponse:
    if request.decision == ReviewDecision.REGENERATE.value:
        try:
            child = register_compilation_regeneration(
                compilation_id,
                stage=request.regenerate_from,
                note=request.note,
                actor=request.actor,
            )
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        start_stage = child.regenerate_from or request.regenerate_from
        await start_longform_workflow(
            str(child.id),
            child.workflow_id,
            start_stage=start_stage,
        )
        return ReviewCompilationResponse(
            compilation_id=compilation_id,
            decision=request.decision,
            child_compilation_id=child.id,
            child_workflow_id=child.workflow_id,
        )

    try:
        review_compilation(
            compilation_id,
            decision=ReviewDecision(request.decision),
            note=request.note,
            actor=request.actor,
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return ReviewCompilationResponse(
        compilation_id=compilation_id,
        decision=request.decision,
    )


@app.post(
    "/v1/compilations/{compilation_id}/publications",
    response_model=PublicationResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def create_compilation_publication(
    compilation_id: uuid.UUID,
    request: CreatePublicationRequest,
) -> Publication:
    _require_youtube_execution()
    try:
        publication = register_compilation_publication(
            compilation_id,
            youtube_connection_id=request.youtube_connection_id,
            title=request.title,
            description=request.description,
            tags=request.tags,
            category_id=request.category_id,
            privacy_status=request.privacy_status,
            publish_at=request.publish_at,
            notify_subscribers=request.notify_subscribers,
            made_for_kids=request.made_for_kids,
            contains_synthetic_media=request.contains_synthetic_media,
        )
    except ValueError as exc:
        code = 404 if "not found" in str(exc) else 409
        raise HTTPException(status_code=code, detail=str(exc)) from exc
    if publication.status == "queued":
        await start_publication_workflow(str(publication.id), publication.workflow_id)
    return publication


@app.get("/v1/publications", response_model=list[PublicationResponse])
def list_publications(
    limit: int = Query(default=50, ge=1, le=250),
    publication_status: str | None = Query(default=None, alias="status"),
) -> list[Publication]:
    with session_scope() as session:
        stmt = select(Publication).order_by(Publication.created_at.desc()).limit(limit)
        if publication_status:
            stmt = stmt.where(Publication.status == publication_status)
        return list(session.scalars(stmt))


@app.get("/v1/publications/{publication_id}", response_model=PublicationResponse)
def get_publication(publication_id: uuid.UUID) -> Publication:
    with session_scope() as session:
        publication = session.get(Publication, publication_id)
        if publication is None:
            raise HTTPException(status_code=404, detail="publication not found")
        return publication


@app.post(
    "/v1/publications/{publication_id}/retry",
    response_model=PublicationResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def retry_youtube_publication(
    publication_id: uuid.UUID,
    request: RetryPublicationRequest,
) -> Publication:
    try:
        publication = retry_publication(
            publication_id,
            allow_new_upload_session=request.allow_new_upload_session,
        )
    except ValueError as exc:
        code = 404 if "not found" in str(exc) else 409
        raise HTTPException(status_code=code, detail=str(exc)) from exc
    await start_publication_workflow(str(publication.id), publication.workflow_id)
    return publication


@app.post(
    "/v1/publications/{publication_id}/analytics/refresh",
    response_model=AnalyticsRefreshResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def refresh_publication_analytics(publication_id: uuid.UUID) -> AnalyticsRefreshResponse:
    with session_scope() as session:
        publication = session.get(Publication, publication_id)
        if publication is None:
            raise HTTPException(status_code=404, detail="publication not found")
        if not publication.youtube_video_id:
            raise HTTPException(status_code=409, detail="publication has no YouTube video ID")
    workflow_id = analytics_refresh_workflow_id(publication_id)
    sample_key = f"manual-{uuid.uuid4().hex}"
    await start_analytics_refresh_workflow(str(publication_id), workflow_id, sample_key)
    return AnalyticsRefreshResponse(
        publication_id=publication_id,
        workflow_id=workflow_id,
        sample_key=sample_key,
    )


@app.get(
    "/v1/publications/{publication_id}/analytics",
    response_model=list[AnalyticsSnapshotDetailResponse],
)
def get_publication_analytics(
    publication_id: uuid.UUID,
    limit: int = Query(default=20, ge=1, le=100),
) -> list[AnalyticsSnapshotDetailResponse]:
    with session_scope() as session:
        if session.get(Publication, publication_id) is None:
            raise HTTPException(status_code=404, detail="publication not found")
        snapshots = list(
            session.scalars(
                select(PublicationAnalyticsSnapshot)
                .where(PublicationAnalyticsSnapshot.publication_id == publication_id)
                .order_by(PublicationAnalyticsSnapshot.sampled_at.desc())
                .limit(limit)
            )
        )
        result: list[AnalyticsSnapshotDetailResponse] = []
        for snapshot in snapshots:
            points = list(
                session.scalars(
                    select(RetentionPoint)
                    .where(RetentionPoint.snapshot_id == snapshot.id)
                    .order_by(RetentionPoint.elapsed_video_time_ratio)
                )
            )
            result.append(
                AnalyticsSnapshotDetailResponse(
                    snapshot=PublicationAnalyticsSnapshotResponse.model_validate(snapshot),
                    retention=[RetentionPointResponse.model_validate(point) for point in points],
                )
            )
        return result


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
