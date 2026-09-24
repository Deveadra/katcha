from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from katcha.api.short_episode_schemas import StartShortEpisodeEditorialRequest
from katcha.api.short_episodes import start_short_episode_editorial
from katcha.api.trends import TrendEvidencePacketResponse, TrendOpportunityResponse
from katcha.db import session_scope
from katcha.services.channel_profiles import ensure_active_profile
from katcha.services.trend_explorer import opportunity_board, opportunity_dossier
from katcha.short_episode_models import ShortEpisode

router = APIRouter(
    prefix="/v1/channels/{channel_profile_id}/trends/explorer", tags=["trend-explorer"]
)


class BoardItem(BaseModel):
    topic: str
    tags: list[str]
    opportunity: TrendOpportunityResponse


class SignalPoint(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    provider_key: str
    external_id: str
    source_kind: str
    source_name: str | None
    canonical_url: str | None
    title: str | None
    observed_at: datetime
    published_at: datetime | None
    metrics: dict[str, float]


class Dossier(BoardItem):
    schema_version: str = "trend-explorer-v1"
    evidence: TrendEvidencePacketResponse | None
    signals: list[SignalPoint]
    truncated: bool
    window_start: datetime
    window_end: datetime
    source_content_is_untrusted: bool = True


class EditorialHandoffRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    episode_id: uuid.UUID = Field(description="Existing planned episode linked to this opportunity")


@router.get("", response_model=list[BoardItem], operation_id="list_channel_trend_board")
def board(channel_profile_id: uuid.UUID, limit: int = Query(default=50, ge=1, le=100)):
    try:
        return opportunity_board(channel_profile_id, limit)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/{opportunity_id}", response_model=Dossier, operation_id="get_trend_dossier")
def dossier(
    channel_profile_id: uuid.UUID,
    opportunity_id: uuid.UUID,
    hours: int = Query(default=168, ge=1, le=720),
    limit: int = Query(default=500, ge=1, le=1000),
):
    try:
        return opportunity_dossier(channel_profile_id, opportunity_id, hours=hours, limit=limit)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/{opportunity_id}/episodes", operation_id="list_trend_episodes")
def episodes(channel_profile_id: uuid.UUID, opportunity_id: uuid.UUID):
    # Validate ownership even when there are no linked episodes.
    dossier(channel_profile_id, opportunity_id, hours=1, limit=1)
    with session_scope() as session:
        rows = session.scalars(
            select(ShortEpisode)
            .where(
                ShortEpisode.channel_profile_id == channel_profile_id,
                ShortEpisode.trend_opportunity_id == opportunity_id,
            )
            .order_by(ShortEpisode.created_at.desc())
            .limit(50)
        )
        return [
            {
                "id": row.id,
                "premise": row.premise,
                "status": row.status,
                "stage": row.stage,
                "evidence_frozen": bool((row.plan_snapshot or {}).get("trend_context")),
            }
            for row in rows
        ]


@router.post(
    "/{opportunity_id}/editorial", status_code=202, operation_id="start_trend_episode_editorial"
)
async def handoff(
    channel_profile_id: uuid.UUID,
    opportunity_id: uuid.UUID,
    request: EditorialHandoffRequest,
):
    with session_scope() as session:
        try:
            ensure_active_profile(session, channel_profile_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        episode = session.get(ShortEpisode, request.episode_id)
        if (
            episode is None
            or episode.channel_profile_id != channel_profile_id
            or episode.trend_opportunity_id != opportunity_id
        ):
            raise HTTPException(status_code=404, detail="linked episode not found in this channel")
        context = (episode.plan_snapshot or {}).get("trend_context") or {}
        if context.get("opportunity_id") != str(opportunity_id):
            raise HTTPException(status_code=409, detail="episode has no frozen trend evidence")
    # Reuse AI enablement, provider checks, state validation, and the durable workflow identity.
    return await start_short_episode_editorial(
        request.episode_id, StartShortEpisodeEditorialRequest(start_stage="script")
    )
