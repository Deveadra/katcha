from __future__ import annotations

import asyncio
import logging
import mimetypes
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from fastapi import Depends, FastAPI, HTTPException, Query, Request, status
from fastapi.responses import RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select, text

from katcha import __version__
from katcha.ai.subscription import subscription_connected
from katcha.api.acquisition import router as acquisition_router
from katcha.api.brands import router as brands_router
from katcha.api.chatgpt import router as chatgpt_router
from katcha.api.clip_library import router as clip_library_router
from katcha.api.codex import router as codex_router
from katcha.api.command_center import router as command_center_router
from katcha.api.control import router as control_router
from katcha.api.control_auth import require_control_token, require_native_channel_body
from katcha.api.edit_blueprints import router as edit_blueprints_router
from katcha.api.explorer import router as explorer_router
from katcha.api.goals import router as goals_router
from katcha.api.integrations import router as integrations_router
from katcha.api.intelligence import router as intelligence_router
from katcha.api.operations import router as operations_router
from katcha.api.packaging import router as packaging_router
from katcha.api.reach import router as reach_router
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
    CreatePassthroughProductionRequest,
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
    RecoverRenderRequest,
    RecoverRenderResponse,
    RenderAttemptResponse,
    RetentionPointResponse,
    RetryPublicationRequest,
    ReviewActionResponse,
    ReviewCompilationRequest,
    ReviewCompilationResponse,
    ReviewProductionRequest,
    SourceResponse,
    StartPublicationRequest,
    UpdatePublicationPlanRequest,
    YouTubeConnectionResponse,
    YouTubeOAuthStartResponse,
)
from katcha.api.short_episodes import router as short_episodes_router
from katcha.api.studio import router as studio_router
from katcha.api.telegram import router as telegram_router
from katcha.api.trends import router as trends_router
from katcha.clip_lifecycle_models import ClipLifecycle
from katcha.config import get_settings
from katcha.db import session_scope
from katcha.domain import (
    AnalysisStatus,
    CompilationStatus,
    ProductionStatus,
    ReviewDecision,
    SourceStatus,
)
from katcha.integrations.storage import ObjectStore
from katcha.integrations.youtube.oauth import (
    YouTubeOAuthError,
    begin_youtube_oauth,
    complete_youtube_oauth,
    youtube_oauth_return_to,
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
from katcha.services.intelligence_automation import (
    advance_processed_handoff_receipt,
    reconcile_authorized_handoff_records,
)
from katcha.services.intelligence_handoff import process_handoff_inbox
from katcha.services.productions import (
    register_regeneration,
    register_short_production,
    register_source_passthrough_production,
    review_production,
)
from katcha.services.publications import (
    analytics_refresh_workflow_id,
    register_compilation_publication,
    register_publication,
    release_publication_for_upload,
    retry_publication,
    update_publication_plan,
)
from katcha.services.render_automation import advance_render_automation
from katcha.services.render_recovery import render_attempts_for_source
from katcha.services.sources import register_source

_logger = logging.getLogger(__name__)


async def _process_pending_handoffs_on_startup() -> None:
    """Drain manually dropped handoff files after the API database is available."""
    try:
        items = await asyncio.to_thread(process_handoff_inbox, limit=50)
        for item in items:
            if item.status == "processed":
                await advance_processed_handoff_receipt(item.receipt)
        await reconcile_authorized_handoff_records(limit=200)
    except Exception:
        # Handoff failures must never prevent Katcha itself from starting. Individual
        # file validation failures are already moved to the failed queue by the
        # handoff service; this guard covers broader storage/database problems.
        _logger.exception("automatic handoff inbox processing failed during startup")


@asynccontextmanager
async def _lifespan(_app: FastAPI) -> AsyncIterator[None]:
    await _process_pending_handoffs_on_startup()
    yield


app = FastAPI(
    title="Katcha API",
    dependencies=[Depends(require_control_token), Depends(require_native_channel_body)],
    version=__version__,
    description="Standalone control plane for Katcha media workflows.",
    lifespan=_lifespan,
)
app.include_router(acquisition_router)
app.include_router(brands_router)
app.include_router(chatgpt_router)
app.include_router(codex_router)
app.include_router(clip_library_router)
app.include_router(command_center_router)
app.include_router(goals_router)
app.include_router(control_router)
app.include_router(edit_blueprints_router)
app.include_router(intelligence_router)
app.include_router(integrations_router)
app.include_router(operations_router)
app.include_router(packaging_router)
app.include_router(reach_router)
app.include_router(short_episodes_router)
app.include_router(studio_router)
app.include_router(telegram_router)
app.include_router(trends_router)
app.include_router(explorer_router)
app.mount("/explorer/assets", StaticFiles(directory=Path(__file__).parents[1] / "web"),
          name="explorer-assets")
app.mount("/editing/assets", StaticFiles(directory=Path(__file__).parents[1] / "web"),
          name="editing-assets")
app.mount("/channels/assets", StaticFiles(directory=Path(__file__).parents[1] / "web"),
          name="channels-assets")
app.mount("/ai/assets", StaticFiles(directory=Path(__file__).parents[1] / "web"),
          name="ai-assets")
app.mount("/operations/assets", StaticFiles(directory=Path(__file__).parents[1] / "web"),
          name="operations-assets")
app.mount("/settings/assets", StaticFiles(directory=Path(__file__).parents[1] / "web"),
          name="settings-assets")


app.mount("/system", StaticFiles(directory=Path(__file__).parents[1] / "web" / "system"),
          name="shared-system")


@app.get("/home", include_in_schema=False)
@app.get("/operations", include_in_schema=False)
def operations_shell(request: Request):
    query = "?" + request.url.query if request.url.query else ""
    return RedirectResponse("/operations/assets/operations.html" + query)


@app.get("/explorer", include_in_schema=False)
def explorer_shell(request: Request):
    query = "?" + request.url.query if request.url.query else ""
    return RedirectResponse("/explorer/assets/index.html" + query)


@app.get("/editing", include_in_schema=False)
def editing_shell(request: Request):
    query = "?" + request.url.query if request.url.query else ""
    return RedirectResponse("/editing/assets/editing.html" + query)


@app.get("/channels", include_in_schema=False)
def channels_shell(request: Request):
    query = "?" + request.url.query if request.url.query else ""
    return RedirectResponse("/channels/assets/channels.html" + query)


@app.get("/ai", include_in_schema=False)
def ai_shell(request: Request):
    query = "?" + request.url.query if request.url.query else ""
    return RedirectResponse("/ai/assets/ai.html" + query)


@app.get("/settings", include_in_schema=False)
def settings_shell(request: Request):
    query = "?" + request.url.query if request.url.query else ""
    return RedirectResponse("/settings/assets/settings.html" + query)


@app.get("/ingestion", include_in_schema=False)
def ingestion_shell(request: Request):
    query = "?" + request.url.query if request.url.query else ""
    return RedirectResponse("/editing/assets/ingestion.html" + query)


@app.get("/clips", include_in_schema=False)
def clips_shell(request: Request):
    query = "?" + request.url.query if request.url.query else ""
    return RedirectResponse("/editing/assets/clips.html" + query)


@app.get("/studio", include_in_schema=False)
def studio_shell(request: Request):
    query = "?" + request.url.query if request.url.query else ""
    return RedirectResponse("/editing/assets/studio.html" + query)


def _require_ai_execution() -> None:
    settings = get_settings()
    if not settings.ai_enabled:
        raise HTTPException(status_code=503, detail="AI execution is disabled")
    if settings.resolved_ai_execution_mode() == "fixture":
        return
    if not (settings.openai_api_key or settings.gemini_api_key or subscription_connected(settings)):
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


@app.get("/v1/runtime/ai")
def ai_runtime() -> dict[str, object]:
    settings = get_settings()
    mode = settings.resolved_ai_execution_mode()
    return {
        "execution_mode": mode,
        "live_routing_mode": settings.ai_live_routing_mode,
        "conversation_provider": settings.conversation_provider,
        "agent_provider": settings.agent_provider,
        "paid_openai_fallback_enabled": settings.allow_paid_openai_fallback,
        "external_provider_calls_enabled": mode == "live",
    }


@app.get("/v1/health/workspace", response_model=HealthResponse)
def workspace_ready() -> HealthResponse:
    """Readiness for the interactive control plane; background automation may still warm."""
    try:
        with session_scope() as session:
            session.execute(text("SELECT 1"))
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"database unavailable: {exc}") from exc
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
def youtube_oauth_start(
    return_to: str | None = Query(default=None, max_length=2048),
) -> YouTubeOAuthStartResponse:
    try:
        return YouTubeOAuthStartResponse(
            authorization_url=begin_youtube_oauth(return_to=return_to)
        )
    except (YouTubeOAuthError, RuntimeError, ValueError) as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


def _youtube_oauth_result_url(
    state: str | None,
    *,
    result: str,
    connection_id: uuid.UUID | None = None,
    message: str | None = None,
) -> str:
    target = youtube_oauth_return_to(state)
    parsed = urlsplit(target)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query["setup"] = "1"
    query["youtube"] = result
    if connection_id is not None:
        query["connection"] = str(connection_id)
    if message:
        query["message"] = message[:500]
    return urlunsplit(
        (parsed.scheme, parsed.netloc, parsed.path, urlencode(query), "")
    )


@app.get(
    "/v1/integrations/youtube/oauth/callback",
    response_class=RedirectResponse,
)
async def youtube_oauth_callback(
    state: str | None = None,
    code: str | None = None,
    error: str | None = None,
) -> RedirectResponse:
    if error:
        return RedirectResponse(
            _youtube_oauth_result_url(
                state,
                result="error",
                message=f"Google OAuth failed: {error}",
            ),
            status_code=status.HTTP_303_SEE_OTHER,
        )
    if not code:
        return RedirectResponse(
            _youtube_oauth_result_url(
                state,
                result="error",
                message="Google OAuth callback did not include a code",
            ),
            status_code=status.HTTP_303_SEE_OTHER,
        )
    try:
        connection = await asyncio.to_thread(
            complete_youtube_oauth,
            state=state or "",
            code=code,
        )
    except (YouTubeOAuthError, RuntimeError, ValueError) as exc:
        return RedirectResponse(
            _youtube_oauth_result_url(
                state,
                result="error",
                message=str(exc),
            ),
            status_code=status.HTTP_303_SEE_OTHER,
        )
    return RedirectResponse(
        _youtube_oauth_result_url(
            state,
            result="connected",
            connection_id=connection.id,
        ),
        status_code=status.HTTP_303_SEE_OTHER,
    )


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
            channel_profile_id=request.channel_profile_id,
            edit_blueprint_key=request.edit_blueprint_key,
        )
    except ValueError as exc:
        code = 404 if "not found" in str(exc) else 409
        raise HTTPException(status_code=code, detail=str(exc)) from exc
    if production.status == ProductionStatus.QUEUED.value:
        await start_production_workflow(str(production.id), production.workflow_id)
    return production


@app.post(
    "/v1/clips/{clip_id}/passthrough-productions",
    response_model=ProductionResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_passthrough_production(
    clip_id: uuid.UUID,
    request: CreatePassthroughProductionRequest,
) -> Production:
    try:
        return register_source_passthrough_production(
            clip_id,
            channel_profile_id=request.channel_profile_id,
            idempotency_key=request.idempotency_key,
            actor=request.actor,
        )
    except ValueError as exc:
        code = 404 if "not found" in str(exc) else 409
        raise HTTPException(status_code=code, detail=str(exc)) from exc


@app.get("/v1/productions", response_model=list[ProductionResponse])
def list_productions(
    limit: int = Query(default=50, ge=1, le=250),
    production_status: str | None = Query(default=None, alias="status"),
    channel_profile_id: uuid.UUID | None = Query(default=None),
) -> list[Production]:
    with session_scope() as session:
        stmt = select(Production).order_by(Production.created_at.desc()).limit(limit)
        if production_status:
            stmt = stmt.where(Production.status == production_status)
        if channel_profile_id:
            stmt = stmt.where(Production.channel_profile_id == channel_profile_id)
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


@app.get(
    "/v1/productions/{production_id}/render-attempts",
    response_model=list[RenderAttemptResponse],
)
def get_production_render_attempts(
    production_id: uuid.UUID,
):
    try:
        return render_attempts_for_source("production", production_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post(
    "/v1/productions/{production_id}/render/recover",
    response_model=RecoverRenderResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def recover_production_render(
    production_id: uuid.UUID,
    request: RecoverRenderRequest,
) -> RecoverRenderResponse:
    try:
        attempts = render_attempts_for_source("production", production_id)
        if not attempts or attempts[-1].status != "dead_letter":
            raise ValueError("production does not have a dead-letter render attempt")
        child = register_regeneration(
            production_id,
            stage="render",
            note=request.note or "Recover dead-letter renderer failure",
            actor=request.actor,
        )
    except ValueError as exc:
        code = 404 if "not found" in str(exc) else 409
        raise HTTPException(status_code=code, detail=str(exc)) from exc

    await start_production_workflow(
        str(child.id),
        child.workflow_id,
        start_stage="render",
    )
    return RecoverRenderResponse(
        source_id=production_id,
        child_source_id=child.id,
        child_workflow_id=child.workflow_id,
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
        if request.decision == ReviewDecision.APPROVE.value:
            automation = advance_render_automation("production", production_id)
            if automation.publication_id and automation.publication_workflow_id:
                await start_publication_workflow(
                    automation.publication_id,
                    automation.publication_workflow_id,
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
            hold_for_packaging=request.hold_for_packaging,
        )
    except ValueError as exc:
        code = 404 if "not found" in str(exc) else 409
        raise HTTPException(status_code=code, detail=str(exc)) from exc
    if publication.status == "queued" and publication.stage != "metadata_hold":
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
            hold_for_packaging=request.hold_for_packaging,
        )
    except ValueError as exc:
        code = 404 if "not found" in str(exc) else 409
        raise HTTPException(status_code=code, detail=str(exc)) from exc
    if publication.status == "queued" and publication.stage != "metadata_hold":
        await start_publication_workflow(str(publication.id), publication.workflow_id)
    return publication


@app.post(
    "/v1/publications/{publication_id}/plan",
    response_model=PublicationResponse,
)
def update_held_publication_plan(
    publication_id: uuid.UUID,
    request: UpdatePublicationPlanRequest,
) -> Publication:
    try:
        return update_publication_plan(
            publication_id,
            publish_mode=request.publish_mode,
            publish_at=request.publish_at,
            notify_subscribers=request.notify_subscribers,
            actor=request.actor,
        )
    except ValueError as exc:
        code = 404 if "not found" in str(exc) else 409
        raise HTTPException(status_code=code, detail=str(exc)) from exc


@app.post(
    "/v1/publications/{publication_id}/start",
    response_model=PublicationResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def start_held_publication(
    publication_id: uuid.UUID,
    request: StartPublicationRequest,
) -> Publication:
    _require_youtube_execution()
    try:
        publication = release_publication_for_upload(
            publication_id,
            actor=request.actor,
        )
    except ValueError as exc:
        code = 404 if "not found" in str(exc) else 409
        raise HTTPException(status_code=code, detail=str(exc)) from exc
    await start_publication_workflow(str(publication.id), publication.workflow_id)
    return publication


@app.get("/v1/publications", response_model=list[PublicationResponse])
def list_publications(
    limit: int = Query(default=50, ge=1, le=250),
    publication_status: str | None = Query(default=None, alias="status"),
    youtube_connection_id: uuid.UUID | None = Query(default=None),
) -> list[Publication]:
    with session_scope() as session:
        stmt = select(Publication).order_by(Publication.created_at.desc())
        if publication_status:
            stmt = stmt.where(Publication.status == publication_status)
        if youtube_connection_id:
            stmt = stmt.where(
                Publication.youtube_connection_id == youtube_connection_id
            )
        return list(session.scalars(stmt.limit(limit)))


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


@app.get("/v1/clips/{clip_id}/media")
def get_clip_media(clip_id: uuid.UUID) -> StreamingResponse:
    with session_scope() as session:
        clip = session.get(Clip, clip_id)
        if clip is None:
            raise HTTPException(status_code=404, detail="clip not found")
        lifecycle = session.get(ClipLifecycle, clip_id)
        if lifecycle is not None and lifecycle.lifecycle_state == "purged":
            raise HTTPException(
                status_code=410,
                detail="clip media was permanently purged; metadata is still retained",
            )
        storage_key = (
            lifecycle.archive_key
            if lifecycle is not None
            and lifecycle.lifecycle_state == "archived"
            and lifecycle.archive_key
            else clip.storage_key
        )
        extension = clip.extension or "mp4"

    store = ObjectStore()
    if not store.exists(storage_key):
        raise HTTPException(status_code=404, detail="stored clip media is missing")
    media_type = mimetypes.guess_type(f"clip.{extension.lstrip('.')}")[0]
    return StreamingResponse(
        store.iter_bytes(storage_key),
        media_type=media_type or "application/octet-stream",
        headers={
            "Cache-Control": "private, max-age=60",
            "Content-Disposition": 'inline; filename="clip-preview"',
        },
    )


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
