from __future__ import annotations

import asyncio
import uuid
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from katcha.api.control_auth import control_actor, require_control_channel, require_control_scope
from katcha.api.schemas import PublicationResponse
from katcha.db import session_scope
from katcha.intelligence_models import ChannelProfile
from katcha.publishing_models import Publication
from katcha.services.publication_plans import (
    change_live_release,
    reconcile_live_release,
    save_publication_draft,
    save_thumbnail,
)

router = APIRouter(prefix="/v1/publications", tags=["publication-plans"])


class ReleasePlanRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    publish_mode: Literal["private", "unlisted", "asap", "scheduled"]
    publish_at: datetime | None = None
    channel_local_time: str | None = Field(default=None, max_length=32)
    expected_version: int = Field(default=0, ge=0)


class ManualDraftRequest(ReleasePlanRequest):
    title: str = Field(min_length=1, max_length=100)
    description: str = Field(default="", max_length=5000)
    tags: list[str] = Field(default_factory=list, max_length=50)
    notify_subscribers: bool = False
    made_for_kids: bool = False
    contains_synthetic_media: bool = False
    late_policy: Literal["hold", "asap"] = "hold"


def authorize_publication(request: Request, publication_id: uuid.UUID) -> None:
    require_control_scope(request, "production:create")
    with session_scope() as session:
        row = session.get(Publication, publication_id)
        if row is None:
            raise HTTPException(404, "Publication not found")
        profile = session.scalar(
            select(ChannelProfile).where(
                ChannelProfile.youtube_connection_id == row.youtube_connection_id
            )
        )
        if profile is None:
            raise HTTPException(409, "Publication has no channel profile")
        require_control_channel(request, profile.id)


@router.post("/{publication_id}/draft", response_model=PublicationResponse)
def save_draft(publication_id: uuid.UUID, request: Request, body: ManualDraftRequest):
    authorize_publication(request, publication_id)
    try:
        return save_publication_draft(
            publication_id, actor=control_actor(request), **body.model_dump()
        )
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@router.post("/{publication_id}/release-plan", response_model=PublicationResponse)
def save_live_plan(publication_id: uuid.UUID, request: Request, body: ReleasePlanRequest):
    authorize_publication(request, publication_id)
    from katcha.api.main import _require_youtube_execution

    _require_youtube_execution()
    try:
        return change_live_release(
            publication_id, actor=control_actor(request), **body.model_dump()
        )
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@router.put("/{publication_id}/thumbnail", response_model=PublicationResponse)
async def upload_thumbnail(publication_id: uuid.UUID, request: Request, expected_version: int):
    authorize_publication(request, publication_id)
    data = bytearray()
    async for block in request.stream():
        data.extend(block)
        if len(data) > 2 * 1024 * 1024:
            raise HTTPException(413, "Choose a JPEG or PNG thumbnail up to 2 MiB")
    try:
        return await asyncio.to_thread(
            save_thumbnail,
            reconcile_live_release,
            publication_id,
            bytes(data),
            expected_version=expected_version,
            actor=control_actor(request),
        )
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@router.post("/{publication_id}/reconcile-release", response_model=PublicationResponse)
def reconcile_release(publication_id: uuid.UUID, request: Request):
    authorize_publication(request, publication_id)
    from katcha.api.main import _require_youtube_execution

    _require_youtube_execution()
    try:
        return reconcile_live_release(publication_id, actor=control_actor(request))
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
