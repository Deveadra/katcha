from __future__ import annotations

import uuid

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import select

from katcha.api.short_episode_schemas import (
    CreateShortEpisodeRequest,
    ShortEpisodeCandidateRequest,
    ShortEpisodeDetailResponse,
    ShortEpisodeResponse,
)
from katcha.db import session_scope
from katcha.services.short_episodes import (
    ShortEpisodeCandidateInput,
    register_short_episode,
    short_episode_items,
)
from katcha.short_episode_models import ShortEpisode

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
            idempotency_key=request.idempotency_key,
        )
        items = short_episode_items(episode.id)
    except ValueError as exc:
        message = str(exc)
        code = 404 if "not found" in message else 409
        raise HTTPException(status_code=code, detail=message) from exc
    return ShortEpisodeDetailResponse(episode=episode, items=items)


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


@router.get("/{short_episode_id}", response_model=ShortEpisodeDetailResponse)
def get_short_episode(short_episode_id: uuid.UUID) -> ShortEpisodeDetailResponse:
    with session_scope() as session:
        episode = session.get(ShortEpisode, short_episode_id)
        if episode is None:
            raise HTTPException(status_code=404, detail="short episode not found")
        session.expunge(episode)
    try:
        items = short_episode_items(short_episode_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return ShortEpisodeDetailResponse(episode=episode, items=items)
