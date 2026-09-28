from __future__ import annotations

import base64
import binascii
import re
import uuid
from typing import Literal

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from katcha.db import session_scope
from katcha.integrations.storage import ObjectStore
from katcha.models import Clip
from katcha.orchestration.client import start_short_episode_editorial_workflow
from katcha.services.channel_brands import brand_for_channel
from katcha.services.channel_profiles import ensure_active_profile
from katcha.services.studio_edits import create_studio_render_generation
from katcha.short_episode_models import ShortEpisode, ShortEpisodeAsset

router = APIRouter(prefix="/v1/studio", tags=["clip-studio"])
_RANGE_RE = re.compile(r"^bytes=(\d*)-(\d*)$")


class StudioClipEdit(BaseModel):
    model_config = ConfigDict(extra="forbid")

    position: int = Field(ge=1, le=7)
    source_start_seconds: float = Field(ge=0, le=3600)
    duration_seconds: float = Field(ge=0.5, le=30)
    native_audio_policy: Literal["retain", "duck", "mute"] = "duck"
    audio_volume: float = Field(default=0.35, ge=0, le=1)
    narration_duck_volume: float = Field(default=0.16, ge=0, le=1)
    transition_before: Literal["cut", "punch_cut", "flash"] = "cut"


class StudioRenderRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    edits: list[StudioClipEdit] = Field(min_length=1, max_length=7)
    adopt_active_brand: bool = True
    actor: str = Field(default="clip-studio", min_length=1, max_length=128)
    note: str | None = Field(default=None, max_length=2000)


class StudioRenderResponse(BaseModel):
    source_episode_id: uuid.UUID
    child_episode_id: uuid.UUID
    workflow_id: str
    status: str


class LogoUploadRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    content_type: Literal["image/png", "image/webp"]
    data_base64: str = Field(min_length=8, max_length=3_000_000)


class LogoUploadResponse(BaseModel):
    storage_key: str
    content_type: str
    size_bytes: int


def _workflow_id(base: str) -> str:
    return f"{base}-editorial-render"


def _stream_object(request: Request, key: str, filename: str) -> StreamingResponse:
    store = ObjectStore()
    if not store.exists(key):
        raise HTTPException(status_code=404, detail="media object is missing")
    metadata = store.stat(key)
    size = int(metadata["size_bytes"])
    if size <= 0:
        raise HTTPException(status_code=409, detail="media object is empty")
    content_type = str(metadata.get("content_type") or "application/octet-stream")
    range_header = request.headers.get("range")
    headers = {
        "Accept-Ranges": "bytes",
        "Cache-Control": "private, max-age=60",
        "Content-Disposition": f'inline; filename="{filename}"',
    }
    if not range_header:
        headers["Content-Length"] = str(size)
        return StreamingResponse(store.iter_bytes(key), media_type=content_type, headers=headers)

    match = _RANGE_RE.fullmatch(range_header.strip())
    if not match:
        raise HTTPException(
            status_code=416,
            detail="unsupported media byte range",
            headers={"Content-Range": f"bytes */{size}"},
        )
    start_text, end_text = match.groups()
    if not start_text and not end_text:
        raise HTTPException(status_code=416, detail="empty media byte range")
    if start_text:
        start = int(start_text)
        end = int(end_text) if end_text else size - 1
    else:
        suffix = int(end_text)
        start = max(0, size - suffix)
        end = size - 1
    if start >= size or start < 0 or end < start:
        raise HTTPException(
            status_code=416,
            detail="media byte range is outside the object",
            headers={"Content-Range": f"bytes */{size}"},
        )
    end = min(end, size - 1)
    headers["Content-Length"] = str(end - start + 1)
    headers["Content-Range"] = f"bytes {start}-{end}/{size}"
    return StreamingResponse(
        store.iter_range(key, start, end),
        status_code=206,
        media_type=content_type,
        headers=headers,
    )


@router.get("/clips/{clip_id}/media")
def stream_source_clip(clip_id: uuid.UUID, request: Request) -> StreamingResponse:
    with session_scope() as session:
        clip = session.get(Clip, clip_id)
        if clip is None:
            raise HTTPException(status_code=404, detail="clip not found")
        key = clip.storage_key
        extension = (clip.extension or "mp4").lstrip(".")
    return _stream_object(request, key, f"clip-{clip_id}.{extension}")


@router.get("/episodes/{episode_id}/media")
def stream_episode_render(episode_id: uuid.UUID, request: Request) -> StreamingResponse:
    with session_scope() as session:
        episode = session.get(ShortEpisode, episode_id)
        if episode is None:
            raise HTTPException(status_code=404, detail="short episode not found")
        asset = session.scalar(
            select(ShortEpisodeAsset)
            .where(
                ShortEpisodeAsset.short_episode_id == episode_id,
                ShortEpisodeAsset.kind == "render",
            )
            .order_by(ShortEpisodeAsset.created_at.desc())
        )
        if asset is None:
            raise HTTPException(status_code=409, detail="episode has no rendered preview yet")
        key = asset.storage_key
        expected_key = str((episode.render_manifest or {}).get("output_key") or "")
        if expected_key and key != expected_key:
            raise HTTPException(
                status_code=409,
                detail="render asset does not match frozen manifest",
            )
    return _stream_object(request, key, f"episode-{episode_id}.mp4")


@router.post(
    "/episodes/{episode_id}/render",
    response_model=StudioRenderResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def render_studio_edits(
    episode_id: uuid.UUID,
    request: StudioRenderRequest,
) -> StudioRenderResponse:
    try:
        child = create_studio_render_generation(
            episode_id,
            [edit.model_dump(mode="json") for edit in request.edits],
            adopt_active_brand=request.adopt_active_brand,
            actor=request.actor,
            note=request.note,
        )
    except ValueError as exc:
        message = str(exc)
        code = 404 if "not found" in message else 409
        raise HTTPException(status_code=code, detail=message) from exc
    workflow_id = _workflow_id(child.workflow_id)
    await start_short_episode_editorial_workflow(
        str(child.id),
        workflow_id,
        start_stage="render",
    )
    return StudioRenderResponse(
        source_episode_id=episode_id,
        child_episode_id=child.id,
        workflow_id=workflow_id,
        status=child.status,
    )


@router.post(
    "/channels/{channel_profile_id}/logo",
    response_model=LogoUploadResponse,
    status_code=status.HTTP_201_CREATED,
)
def upload_channel_logo(
    channel_profile_id: uuid.UUID,
    request: LogoUploadRequest,
) -> LogoUploadResponse:
    with session_scope() as session:
        ensure_active_profile(session, channel_profile_id)
    try:
        data = base64.b64decode(request.data_base64, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise HTTPException(status_code=400, detail="logo payload is not valid base64") from exc
    if len(data) > 2 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="logo must be 2 MB or smaller")

    if request.content_type == "image/png":
        if not data.startswith(b"\x89PNG\r\n\x1a\n"):
            raise HTTPException(status_code=400, detail="logo payload is not a PNG")
        extension = "png"
    else:
        if len(data) < 12 or data[:4] != b"RIFF" or data[8:12] != b"WEBP":
            raise HTTPException(status_code=400, detail="logo payload is not a WebP image")
        extension = "webp"

    key = f"brands/{channel_profile_id}/logos/{uuid.uuid4().hex}.{extension}"
    store = ObjectStore()
    store.ensure_bucket()
    store.put_bytes(data, key, content_type=request.content_type)
    return LogoUploadResponse(
        storage_key=key,
        content_type=request.content_type,
        size_bytes=len(data),
    )


@router.get("/channels/{channel_profile_id}/logo/media")
def stream_active_channel_logo(
    channel_profile_id: uuid.UUID,
    request: Request,
) -> StreamingResponse:
    with session_scope() as session:
        contract, _ = brand_for_channel(session, channel_profile_id)
        logo = dict(contract.visual.get("logo") or {})
        key = str(logo.get("storage_key") or "")
        if not logo.get("enabled") or not key:
            raise HTTPException(status_code=404, detail="active channel brand has no logo")
    return _stream_object(request, key, f"channel-{channel_profile_id}-logo")
