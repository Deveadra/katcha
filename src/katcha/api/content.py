from __future__ import annotations

import asyncio
import uuid
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field, HttpUrl
from sqlalchemy import select

from katcha.api.control_auth import (
    control_actor,
    control_allowed_channel_ids,
    require_control_channel,
    require_control_scope,
)
from katcha.api.schemas import ProductionResponse, PublicationResponse
from katcha.content_models import ContentItem
from katcha.db import session_scope
from katcha.intelligence_models import ChannelProfile
from katcha.models import Clip, SourceItem
from katcha.orchestration.client import start_ingest_workflow, start_production_workflow
from katcha.production_models import Production
from katcha.services import content
from katcha.services.channel_profiles import ensure_active_profile
from katcha.services.clip_lifecycle import channel_ids_for_clip
from katcha.services.productions import (
    register_short_production,
)
from katcha.services.publications import register_publication
from katcha.services.sources import register_source

router = APIRouter(prefix="/v1/channels/{channel_profile_id}/content", tags=["manual-content"])


class IntakeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_key: str = Field(min_length=1, max_length=160)
    input_kind: Literal["file", "url", "clip"]
    title: str = Field(min_length=1, max_length=500)
    filename: str | None = Field(default=None, max_length=255)
    size_bytes: int | None = Field(default=None, ge=1, le=content.MAX_FILE_BYTES)
    rights_confirmed: bool = False
    fingerprint: str | None = Field(default=None, max_length=200)
    url: HttpUrl | None = None
    clip_id: uuid.UUID | None = None


class PrepareRequest(BaseModel):
    mode: Literal["preserve", "ai_short"] = "preserve"


class DraftPublicationRequest(BaseModel):
    production_id: uuid.UUID
    title: str = Field(min_length=1, max_length=100)


def _authorize(request: Request, channel_id: uuid.UUID, scope: str = "channels:read") -> None:
    require_control_channel(request, channel_id)
    require_control_scope(request, scope)
    with session_scope() as session:
        profile = session.get(ChannelProfile, channel_id)
        if profile is None:
            raise HTTPException(404, "Channel not found")
        if scope != "channels:read" and profile.status != "active":
            raise HTTPException(409, "Resume this channel before changing content")


def _get_item(channel_id: uuid.UUID, item_id: uuid.UUID) -> ContentItem:
    with session_scope() as session:
        item = session.scalar(
            select(ContentItem).where(
                ContentItem.id == item_id, ContentItem.channel_profile_id == channel_id
            )
        )
        if item is None:
            raise HTTPException(404, "Content not found in this channel")
        session.expunge(item)
        return item


@router.get("")
def list_items(
    channel_profile_id: uuid.UUID,
    request: Request,
    q: str = Query(default="", max_length=300),
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=25, ge=1, le=100),
):
    _authorize(request, channel_profile_id)
    try:
        return content.list_content(channel_profile_id, q=q, offset=offset, limit=limit)
    except ValueError as exc:
        raise HTTPException(404 if "not found" in str(exc) else 409, str(exc)) from exc


@router.post("", status_code=201)
async def intake(channel_profile_id: uuid.UUID, request: Request, body: IntakeRequest):
    _authorize(request, channel_profile_id, "channels:write")
    if not body.rights_confirmed:
        raise HTTPException(422, "Confirm your authorization to publish this media and its audio")
    with session_scope() as session:
        ensure_active_profile(session, channel_profile_id)
    details: dict = {
        "fingerprint": body.fingerprint,
        "rights_confirmed": True,
        "rights_confirmed_by": control_actor(request),
    }
    source_id = None
    if body.input_kind == "file":
        if not body.filename or not body.size_bytes:
            raise HTTPException(422, "Choose a non-empty video file")
        if Path(body.filename).suffix.lower() not in {".mp4", ".webm", ".mov", ".mkv"}:
            raise HTTPException(415, "Choose an MP4, WebM, MOV or MKV video export")
    elif body.input_kind == "clip":
        with session_scope() as session:
            if not body.clip_id or session.get(Clip, body.clip_id) is None:
                raise HTTPException(404, "Clip not found")
            owners = channel_ids_for_clip(session, body.clip_id)
            if channel_profile_id not in owners and (
                owners or control_allowed_channel_ids(request) is not None
            ):
                raise HTTPException(403, "Clip is not available to this channel")
        details["fingerprint"] = str(body.clip_id)
    else:
        if body.url is None or body.url.username or body.url.password:
            raise HTTPException(422, "Paste a public video link without embedded credentials")
        details["url"] = str(body.url)
        details["fingerprint"] = str(body.url)
        source = register_source(str(body.url))
        source_id = source.id
    try:
        row = content.create_content(
            channel_profile_id,
            request_key=body.request_key,
            input_kind=body.input_kind,
            title=body.title,
            filename=body.filename,
            size_bytes=body.size_bytes,
            clip_id=body.clip_id,
            source_id=source_id,
            details=details,
        )
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    if source_id:
        with session_scope() as session:
            source = session.get(SourceItem, source_id)
            workflow_id, pending = source.workflow_id, source.status == "registered"
        if pending:
            try:
                await start_ingest_workflow(str(source_id), workflow_id)
            except Exception:
                with session_scope() as session:
                    saved = session.get(ContentItem, row.id)
                    saved.error = (
                        "Download saved; worker dispatch could not be confirmed. Retry download."
                    )
    with session_scope() as session:
        return content.content_detail(session, session.get(ContentItem, row.id))


@router.get("/{item_id}")
def detail(channel_profile_id: uuid.UUID, item_id: uuid.UUID, request: Request):
    _authorize(request, channel_profile_id)
    with session_scope() as session:
        row = session.scalar(
            select(ContentItem).where(
                ContentItem.id == item_id, ContentItem.channel_profile_id == channel_profile_id
            )
        )
        if row is None:
            raise HTTPException(404, "Content not found in this channel")
        return content.content_detail(session, row)


@router.put("/{item_id}/chunks")
async def upload_chunk(
    channel_profile_id: uuid.UUID,
    item_id: uuid.UUID,
    request: Request,
    offset: int = Query(ge=0),
):
    _authorize(request, channel_profile_id, "channels:write")
    _get_item(channel_profile_id, item_id)
    data = bytearray()
    async for block in request.stream():
        data.extend(block)
        if len(data) > content.CHUNK_BYTES:
            raise HTTPException(413, "Upload chunks must be 8 MiB or smaller")
    try:
        return await asyncio.to_thread(
            content.accept_chunk, channel_profile_id, item_id, offset, bytes(data)
        )
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@router.post("/{item_id}/complete")
async def complete_upload(channel_profile_id: uuid.UUID, item_id: uuid.UUID, request: Request):
    _authorize(request, channel_profile_id, "channels:write")
    _get_item(channel_profile_id, item_id)
    try:
        result = await asyncio.to_thread(content.finish_upload, channel_profile_id, item_id)
        await asyncio.to_thread(content.cleanup_completed_upload, channel_profile_id, item_id)
        return result
    except Exception as exc:
        with session_scope() as session:
            row = session.get(ContentItem, item_id)
            row.error = "Video validation failed; check the export and retry completion."
        raise HTTPException(
            409,
            "Video validation failed; uploaded bytes retained. "
            "Check your export and retry completion.",
        ) from exc


@router.post("/{item_id}/retry-download")
async def retry_download(channel_profile_id: uuid.UUID, item_id: uuid.UUID, request: Request):
    _authorize(request, channel_profile_id, "channels:write")
    row = _get_item(channel_profile_id, item_id)
    if not row.source_id:
        raise HTTPException(409, "This item has no remote download")
    with session_scope() as session:
        source = session.get(SourceItem, row.source_id)
        url = source.source_url
        if source.status not in {"registered", "failed"}:
            raise HTTPException(409, "Download is already running or media is ready")
    source = register_source(url, force_retry=True)
    await start_ingest_workflow(str(source.id), source.workflow_id)
    with session_scope() as session:
        session.get(ContentItem, item_id).error = None
    return {"status": "download_queued"}


@router.post("/{item_id}/prepare", response_model=ProductionResponse)
async def prepare(
    channel_profile_id: uuid.UUID,
    item_id: uuid.UUID,
    request: Request,
    body: PrepareRequest,
):
    _authorize(request, channel_profile_id, "production:create")
    row = _get_item(channel_profile_id, item_id)
    with session_scope() as session:
        source = session.get(SourceItem, row.source_id) if row.source_id else None
        clip_id = row.clip_id or (source.clip_id if source else None)
    if not clip_id:
        raise HTTPException(409, "Wait for the media download or upload to finish")
    try:
        if body.mode == "preserve":
            production = await asyncio.to_thread(
                content.prepare_unchanged_video, channel_profile_id, item_id, control_actor(request)
            )
        else:
            from katcha.api.main import _require_ai_execution

            _require_ai_execution()
            production = register_short_production(
                clip_id,
                channel_profile_id=channel_profile_id,
                idempotency_key=f"manual-ai-{item_id}",
            )
            if production.status == "queued":
                await start_production_workflow(str(production.id), production.workflow_id)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    with session_scope() as session:
        session.get(ContentItem, item_id).production_id = production.id
    return production


@router.post("/{item_id}/publication", response_model=PublicationResponse)
def publication_draft(
    channel_profile_id: uuid.UUID,
    item_id: uuid.UUID,
    request: Request,
    body: DraftPublicationRequest,
):
    _authorize(request, channel_profile_id, "production:create")
    row = _get_item(channel_profile_id, item_id)
    with session_scope() as session:
        profile = ensure_active_profile(session, channel_profile_id)
        source = session.get(SourceItem, row.source_id) if row.source_id else None
        clip_id = row.clip_id or (source.clip_id if source else None)
        production = session.get(Production, body.production_id)
        if (
            production is None
            or production.channel_profile_id != channel_profile_id
            or production.clip_id != clip_id
        ):
            raise HTTPException(404, "Production not found for this content and channel")
        connection_id = profile.youtube_connection_id
    try:
        return register_publication(
            body.production_id,
            youtube_connection_id=connection_id,
            title=body.title,
            privacy_status="private",
            hold_for_packaging=True,
        )
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@router.get("/packages/history")
def package_history(
    channel_profile_id: uuid.UUID,
    request: Request,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=25, ge=1, le=100),
):
    from sqlalchemy import func

    from katcha.acquisition_models import IntelligenceIngestBatch

    _authorize(request, channel_profile_id)
    with session_scope() as session:
        query = select(IntelligenceIngestBatch).where(
            IntelligenceIngestBatch.channel_profile_id == channel_profile_id
        )
        total = session.scalar(select(func.count()).select_from(query.subquery())) or 0
        rows = session.scalars(
            query.order_by(IntelligenceIngestBatch.created_at.desc(), IntelligenceIngestBatch.id)
            .offset(offset)
            .limit(limit)
        )
        return {
            "total": total,
            "offset": offset,
            "limit": limit,
            "items": [
                {
                    "id": row.id,
                    "batch_key": row.batch_key,
                    "producer": row.producer,
                    "source_type": row.source_type,
                    "record_count": row.record_count,
                    "created_at": row.created_at,
                    "content_sha256": row.content_sha256,
                }
                for row in rows
            ],
        }
