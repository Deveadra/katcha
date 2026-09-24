from __future__ import annotations

import uuid

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import select

from katcha.api.schemas import (
    CreatePublicationRequest,
    PublicationResponse,
    RecoverRenderRequest,
    RecoverRenderResponse,
    RenderAttemptResponse,
)
from katcha.api.short_episode_schemas import (
    CreateShortEpisodeRequest,
    ReviewShortEpisodeRequest,
    ReviewShortEpisodeResponse,
    ShortEpisodeAssetResponse,
    ShortEpisodeCandidateRequest,
    ShortEpisodeDetailResponse,
    ShortEpisodeResponse,
    ShortEpisodeReviewResponse,
    ShortEpisodeScriptResponse,
    StartShortEpisodeEditorialRequest,
    StartShortEpisodeEditorialResponse,
)
from katcha.config import get_settings
from katcha.db import session_scope
from katcha.domain import ReviewDecision
from katcha.orchestration.client import (
    start_publication_workflow,
    start_short_episode_editorial_workflow,
)
from katcha.publishing_models import Publication
from katcha.services.publications import register_short_episode_publication
from katcha.services.render_automation import advance_render_automation
from katcha.services.render_recovery import render_attempts_for_source
from katcha.services.short_episode_reviews import (
    register_short_episode_regeneration,
    review_short_episode,
)
from katcha.services.short_episodes import (
    ShortEpisodeCandidateInput,
    register_short_episode,
    short_episode_items,
)
from katcha.short_episode_models import (
    ShortEpisode,
    ShortEpisodeAsset,
    ShortEpisodeReview,
    ShortEpisodeScript,
)

router = APIRouter(prefix="/v1/short-episodes", tags=["short-episodes"])


def _candidate_input(raw: ShortEpisodeCandidateRequest) -> ShortEpisodeCandidateInput:
    return ShortEpisodeCandidateInput(
        clip_id=raw.clip_id,
        hook_strength=raw.hook_strength,
        visual_clarity=raw.visual_clarity,
        payoff_strength=raw.payoff_strength,
        escalation_value=raw.escalation_value,
        commentary_opportunity=raw.commentary_opportunity,
        novelty=raw.novelty,
        source_quality=raw.source_quality,
    )


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


def _editorial_workflow_id(episode_workflow_id: str, start_stage: str) -> str:
    """Keep planning identity distinct from a stage-specific Temporal execution."""
    return f"{episode_workflow_id}-editorial-{start_stage}"


def _episode_detail(episode: ShortEpisode) -> ShortEpisodeDetailResponse:
    items = short_episode_items(episode.id)
    with session_scope() as session:
        scripts = list(
            session.scalars(
                select(ShortEpisodeScript)
                .where(ShortEpisodeScript.short_episode_id == episode.id)
                .order_by(ShortEpisodeScript.candidate_index)
            )
        )
        assets = list(
            session.scalars(
                select(ShortEpisodeAsset)
                .where(ShortEpisodeAsset.short_episode_id == episode.id)
                .order_by(ShortEpisodeAsset.kind)
            )
        )
        reviews = list(
            session.scalars(
                select(ShortEpisodeReview)
                .where(ShortEpisodeReview.short_episode_id == episode.id)
                .order_by(ShortEpisodeReview.created_at)
            )
        )
        return ShortEpisodeDetailResponse(
            episode=ShortEpisodeResponse.model_validate(episode),
            items=items,
            scripts=[ShortEpisodeScriptResponse.model_validate(script) for script in scripts],
            assets=[ShortEpisodeAssetResponse.model_validate(asset) for asset in assets],
            reviews=[ShortEpisodeReviewResponse.model_validate(review) for review in reviews],
        )


@router.post(
    "",
    response_model=ShortEpisodeDetailResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_short_episode(request: CreateShortEpisodeRequest) -> ShortEpisodeDetailResponse:
    try:
        episode = register_short_episode(
            channel_profile_id=request.channel_profile_id,
            premise=request.premise,
            candidates=[_candidate_input(candidate) for candidate in request.candidates],
            item_count=request.item_count,
            format_key=request.format_key,
            format_version=request.format_version,
            edit_blueprint_key=request.edit_blueprint_key,
            idempotency_key=request.idempotency_key,
            trend_opportunity_id=request.trend_opportunity_id,
        )
        return _episode_detail(episode)
    except ValueError as exc:
        message = str(exc)
        code = 404 if "not found" in message else 409
        raise HTTPException(status_code=code, detail=message) from exc


@router.get("", response_model=list[ShortEpisodeResponse])
def list_short_episodes(
    limit: int = Query(default=50, ge=1, le=250),
    channel_profile_id: uuid.UUID | None = Query(default=None),
    episode_status: str | None = Query(default=None, alias="status"),
) -> list[ShortEpisode]:
    with session_scope() as session:
        stmt = select(ShortEpisode).order_by(ShortEpisode.created_at.desc()).limit(limit)
        if channel_profile_id is not None:
            stmt = stmt.where(ShortEpisode.channel_profile_id == channel_profile_id)
        if episode_status is not None:
            stmt = stmt.where(ShortEpisode.status == episode_status)
        return list(session.scalars(stmt))


@router.get(
    "/{short_episode_id}/render-attempts",
    response_model=list[RenderAttemptResponse],
)
def get_short_episode_render_attempts(
    short_episode_id: uuid.UUID,
):
    try:
        return render_attempts_for_source("short_episode", short_episode_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post(
    "/{short_episode_id}/render/recover",
    response_model=RecoverRenderResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def recover_short_episode_render(
    short_episode_id: uuid.UUID,
    request: RecoverRenderRequest,
) -> RecoverRenderResponse:
    try:
        attempts = render_attempts_for_source("short_episode", short_episode_id)
        if not attempts or attempts[-1].status != "dead_letter":
            raise ValueError("short episode does not have a dead-letter render attempt")
        child = register_short_episode_regeneration(
            short_episode_id,
            stage="render",
            note=request.note or "Recover dead-letter renderer failure",
            actor=request.actor,
        )
    except ValueError as exc:
        code = 404 if "not found" in str(exc) else 409
        raise HTTPException(status_code=code, detail=str(exc)) from exc

    child_workflow_id = _editorial_workflow_id(child.workflow_id, "render")
    await start_short_episode_editorial_workflow(
        str(child.id),
        child_workflow_id,
        start_stage="render",
    )
    return RecoverRenderResponse(
        source_id=short_episode_id,
        child_source_id=child.id,
        child_workflow_id=child_workflow_id,
    )


@router.post(
    "/{short_episode_id}/editorial",
    response_model=StartShortEpisodeEditorialResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def start_short_episode_editorial(
    short_episode_id: uuid.UUID,
    request: StartShortEpisodeEditorialRequest,
) -> StartShortEpisodeEditorialResponse:
    if request.start_stage in {"script", "voice"}:
        _require_ai_execution()
    with session_scope() as session:
        episode = session.get(ShortEpisode, short_episode_id)
        if episode is None:
            raise HTTPException(status_code=404, detail="short episode not found")
        if episode.status == "failed":
            raise HTTPException(
                status_code=409,
                detail="failed episodes require a new regeneration lineage before retry",
            )
        if request.start_stage == "voice" and episode.selected_script_id is None:
            raise HTTPException(
                status_code=409,
                detail="voice stage requires an already selected episode script",
            )
        if request.start_stage == "render" and episode.status != "editorial_approved":
            raise HTTPException(
                status_code=409,
                detail="render stage requires editorial approval of the voiced episode",
            )
        workflow_id = _editorial_workflow_id(episode.workflow_id, request.start_stage)
        current_status = episode.status

    await start_short_episode_editorial_workflow(
        str(short_episode_id),
        workflow_id,
        start_stage=request.start_stage,
    )
    return StartShortEpisodeEditorialResponse(
        episode_id=short_episode_id,
        workflow_id=workflow_id,
        start_stage=request.start_stage,
        status=current_status,
    )


@router.post(
    "/{short_episode_id}/review",
    response_model=ReviewShortEpisodeResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def review_ranked_short_episode(
    short_episode_id: uuid.UUID,
    request: ReviewShortEpisodeRequest,
) -> ReviewShortEpisodeResponse:
    if request.decision == ReviewDecision.REGENERATE.value:
        if request.regenerate_from in {"script", "voice"}:
            _require_ai_execution()
        try:
            child = register_short_episode_regeneration(
                short_episode_id,
                stage=request.regenerate_from,
                note=request.note,
                actor=request.actor,
            )
        except ValueError as exc:
            message = str(exc)
            code = 404 if "not found" in message else 409
            raise HTTPException(status_code=code, detail=message) from exc
        child_workflow_id = _editorial_workflow_id(
            child.workflow_id, request.regenerate_from
        )
        await start_short_episode_editorial_workflow(
            str(child.id),
            child_workflow_id,
            start_stage=request.regenerate_from,
        )
        return ReviewShortEpisodeResponse(
            episode_id=short_episode_id,
            decision=request.decision,
            child_episode_id=child.id,
            child_workflow_id=child_workflow_id,
        )

    try:
        review_short_episode(
            short_episode_id,
            decision=ReviewDecision(request.decision),
            note=request.note,
            actor=request.actor,
        )
        with session_scope() as session:
            episode = session.get(ShortEpisode, short_episode_id)
            if episode is None:
                raise ValueError(f"short episode not found: {short_episode_id}")
            current_status = episode.status
            current_stage = episode.stage
            episode_workflow_id = episode.workflow_id

        if (
            request.decision == ReviewDecision.APPROVE.value
            and current_status == "editorial_approved"
        ):
            render_workflow_id = _editorial_workflow_id(
                episode_workflow_id,
                "render",
            )
            await start_short_episode_editorial_workflow(
                str(short_episode_id),
                render_workflow_id,
                start_stage="render",
            )
        elif (
            request.decision == ReviewDecision.APPROVE.value
            and current_status == "approved"
            and current_stage == "render_approved"
        ):
            automation = advance_render_automation(
                "short_episode",
                short_episode_id,
            )
            if automation.publication_id and automation.publication_workflow_id:
                await start_publication_workflow(
                    automation.publication_id,
                    automation.publication_workflow_id,
                )
    except ValueError as exc:
        message = str(exc)
        code = 404 if "not found" in message else 409
        raise HTTPException(status_code=code, detail=message) from exc
    return ReviewShortEpisodeResponse(
        episode_id=short_episode_id,
        decision=request.decision,
    )


@router.post(
    "/{short_episode_id}/publications",
    response_model=PublicationResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def create_short_episode_publication(
    short_episode_id: uuid.UUID,
    request: CreatePublicationRequest,
) -> Publication:
    _require_youtube_execution()
    try:
        publication = register_short_episode_publication(
            short_episode_id,
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
        message = str(exc)
        code = 404 if "not found" in message else 409
        raise HTTPException(status_code=code, detail=message) from exc
    if publication.status == "queued":
        await start_publication_workflow(str(publication.id), publication.workflow_id)
    return publication


@router.get("/{short_episode_id}", response_model=ShortEpisodeDetailResponse)
def get_short_episode(short_episode_id: uuid.UUID) -> ShortEpisodeDetailResponse:
    with session_scope() as session:
        episode = session.get(ShortEpisode, short_episode_id)
        if episode is None:
            raise HTTPException(status_code=404, detail="short episode not found")
        session.expunge(episode)
    try:
        return _episode_detail(episode)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
