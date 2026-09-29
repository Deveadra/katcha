from __future__ import annotations

import shutil
import uuid
from pathlib import Path
from typing import Annotated, Literal

from fastapi import (
    APIRouter,
    BackgroundTasks,
    File,
    Form,
    HTTPException,
    Query,
    UploadFile,
    status,
)
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from katcha.config import get_settings
from katcha.db import session_scope
from katcha.external_edit_models import ExternalEditHandoff
from katcha.services.elevenlabs_integration import (
    ElevenLabsIntegrationError,
    elevenlabs_status,
    generate_elevenlabs_preview,
    get_elevenlabs_voice,
    list_elevenlabs_models,
    resolve_elevenlabs_voice,
    search_elevenlabs_voices,
)
from katcha.services.external_edit import (
    adopt_external_output,
    build_handoff_zip,
    handoff_manifest,
    import_external_output,
    prepare_invideo_handoff,
)
from katcha.services.provider_settings import (
    get_channel_provider_setting,
    upsert_channel_provider_setting,
)

router = APIRouter(prefix="/v1/integrations", tags=["integrations"])


class IntegrationProviderStatus(BaseModel):
    provider: str
    capability: str
    configured: bool
    mode: str
    detail: str


class ElevenLabsStatusResponse(BaseModel):
    configured: bool
    connected: bool
    voice_id: str | None
    voice_name: str | None
    voice_category: str | None = None
    voice_labels: dict[str, object] = Field(default_factory=dict)
    model_id: str
    output_format: str
    subscription: dict[str, object] | None
    detail: str


class ElevenLabsVoiceResponse(BaseModel):
    voice_id: str | None
    name: str | None
    category: str | None
    description: str | None
    labels: dict[str, object] = Field(default_factory=dict)
    preview_url: str | None
    is_owner: bool | None
    is_legacy: bool | None


class ElevenLabsVoicePage(BaseModel):
    voices: list[ElevenLabsVoiceResponse]
    has_more: bool
    total_count: int | None
    next_page_token: str | None


class ElevenLabsModelResponse(BaseModel):
    model_id: str | None
    name: str | None
    description: str | None
    maximum_text_length_per_request: int | None = None
    token_cost_factor: float | None = None


class ElevenLabsChannelConfigResponse(BaseModel):
    channel_profile_id: uuid.UUID
    enabled: bool
    voice_id: str | None
    voice_name: str | None
    model_id: str
    model_name: str | None
    source: Literal["channel", "global", "unset"]


class ElevenLabsChannelConfigUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    voice_id: str = Field(min_length=1, max_length=255)
    model_id: str = Field(min_length=1, max_length=255)
    actor: str = Field(default="operator", min_length=1, max_length=128)


class ElevenLabsPreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1, max_length=600)
    voice_id: str | None = Field(default=None, min_length=1, max_length=255)
    model_id: str | None = Field(default=None, min_length=1, max_length=255)
    stability: float = Field(default=0.42, ge=0, le=1)
    similarity_boost: float = Field(default=0.75, ge=0, le=1)
    style: float = Field(default=0.0, ge=0, le=1)
    speed: float = Field(default=1.03, ge=0.7, le=1.2)


class InVideoHandoffCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_type: Literal["production", "short_episode"]
    source_id: uuid.UUID
    actor: str = Field(default="operator", min_length=1, max_length=128)
    note: str | None = Field(default=None, max_length=2000)


class InVideoHandoffAction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    actor: str = Field(default="operator", min_length=1, max_length=128)


class InVideoHandoffResponse(BaseModel):
    id: uuid.UUID
    provider: str
    source_type: str
    source_id: uuid.UUID
    generation: int
    status: str
    package_manifest_key: str
    output_key: str | None
    external_project_id: str | None
    handoff_metadata: dict[str, object]


def _response(row: ExternalEditHandoff) -> InVideoHandoffResponse:
    return InVideoHandoffResponse(
        id=row.id,
        provider=row.provider,
        source_type=row.source_type,
        source_id=row.source_id,
        generation=row.generation,
        status=row.status,
        package_manifest_key=row.package_manifest_key,
        output_key=row.output_key,
        external_project_id=row.external_project_id,
        handoff_metadata=dict(row.handoff_metadata or {}),
    )


@router.get("/providers", response_model=list[IntegrationProviderStatus])
def integration_provider_status() -> list[IntegrationProviderStatus]:
    settings = get_settings()
    elevenlabs_configured = bool(settings.elevenlabs_api_key)
    return [
        IntegrationProviderStatus(
            provider="elevenlabs",
            capability="text_to_speech",
            configured=elevenlabs_configured,
            mode="api",
            detail=(
                "ElevenLabs API is configured. Select voices per channel in Channel Studio."
                if elevenlabs_configured
                else "Set KATCHA_ELEVENLABS_API_KEY, then select a channel voice."
            ),
        ),
        IntegrationProviderStatus(
            provider="invideo",
            capability="external_edit",
            configured=True,
            mode="manual_bridge",
            detail=(
                "Katcha prepares a tracked edit package and re-imports the verified output. "
                "Direct project automation stays disabled until a documented InVideo API "
                "contract is available for the account."
            ),
        ),
    ]


@router.get(
    "/elevenlabs/status",
    response_model=ElevenLabsStatusResponse,
)
def get_elevenlabs_status(
    channel_profile_id: uuid.UUID | None = Query(default=None),
) -> ElevenLabsStatusResponse:
    try:
        return ElevenLabsStatusResponse.model_validate(
            elevenlabs_status(channel_profile_id=channel_profile_id)
        )
    except ElevenLabsIntegrationError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.get(
    "/elevenlabs/voices",
    response_model=ElevenLabsVoicePage,
)
def list_elevenlabs_voices(
    search: str | None = Query(default=None, max_length=120),
    page_size: int = Query(default=25, ge=1, le=100),
    next_page_token: str | None = Query(default=None, max_length=500),
) -> ElevenLabsVoicePage:
    try:
        return ElevenLabsVoicePage.model_validate(
            search_elevenlabs_voices(
                search=search,
                page_size=page_size,
                next_page_token=next_page_token,
            )
        )
    except ElevenLabsIntegrationError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.get(
    "/elevenlabs/models",
    response_model=list[ElevenLabsModelResponse],
)
def get_elevenlabs_models() -> list[ElevenLabsModelResponse]:
    try:
        return [
            ElevenLabsModelResponse.model_validate(row)
            for row in list_elevenlabs_models()
        ]
    except ElevenLabsIntegrationError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


def _channel_elevenlabs_response(
    channel_profile_id: uuid.UUID,
    *,
    voice_name: str | None = None,
    model_name: str | None = None,
) -> ElevenLabsChannelConfigResponse:
    row = get_channel_provider_setting(channel_profile_id, "elevenlabs")
    voice_id, model_id = resolve_elevenlabs_voice(
        channel_profile_id=channel_profile_id,
    )
    if row is not None and row.enabled:
        source: Literal["channel", "global", "unset"] = "channel"
        config = dict(row.config or {})
        voice_name = voice_name or str(config.get("voice_name") or "") or None
        model_name = model_name or str(config.get("model_name") or "") or None
    elif voice_id:
        source = "global"
    else:
        source = "unset"
    return ElevenLabsChannelConfigResponse(
        channel_profile_id=channel_profile_id,
        enabled=bool(row.enabled) if row is not None else bool(voice_id),
        voice_id=voice_id,
        voice_name=voice_name,
        model_id=model_id,
        model_name=model_name,
        source=source,
    )


@router.get(
    "/elevenlabs/channels/{channel_profile_id}",
    response_model=ElevenLabsChannelConfigResponse,
)
def get_channel_elevenlabs_config(
    channel_profile_id: uuid.UUID,
) -> ElevenLabsChannelConfigResponse:
    return _channel_elevenlabs_response(channel_profile_id)


@router.put(
    "/elevenlabs/channels/{channel_profile_id}",
    response_model=ElevenLabsChannelConfigResponse,
)
def update_channel_elevenlabs_config(
    channel_profile_id: uuid.UUID,
    request: ElevenLabsChannelConfigUpdate,
) -> ElevenLabsChannelConfigResponse:
    try:
        voice = get_elevenlabs_voice(request.voice_id)
        models = list_elevenlabs_models()
        model = next(
            (row for row in models if row.get("model_id") == request.model_id),
            None,
        )
        if model is None:
            raise ValueError(
                f"ElevenLabs model is not available for TTS: {request.model_id}"
            )
        upsert_channel_provider_setting(
            channel_profile_id,
            provider="elevenlabs",
            enabled=request.enabled,
            config={
                "voice_id": request.voice_id,
                "model_id": request.model_id,
                "voice_name": voice.get("name"),
                "model_name": model.get("name"),
            },
            actor=request.actor,
        )
        return _channel_elevenlabs_response(
            channel_profile_id,
            voice_name=str(voice.get("name") or "") or None,
            model_name=str(model.get("name") or "") or None,
        )
    except ElevenLabsIntegrationError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except ValueError as exc:
        code = 404 if "channel profile not found" in str(exc) else 400
        raise HTTPException(status_code=code, detail=str(exc)) from exc


@router.post("/elevenlabs/channels/{channel_profile_id}/preview")
def preview_channel_elevenlabs_voice(
    channel_profile_id: uuid.UUID,
    request: ElevenLabsPreviewRequest,
) -> Response:
    saved_voice_id, saved_model_id = resolve_elevenlabs_voice(
        channel_profile_id=channel_profile_id,
    )
    voice_id = request.voice_id or saved_voice_id
    model_id = request.model_id or saved_model_id
    if not voice_id:
        raise HTTPException(
            status_code=400,
            detail="Select an ElevenLabs voice before generating a preview.",
        )
    try:
        audio, metadata = generate_elevenlabs_preview(
            request.text,
            voice_id=voice_id,
            model_id=model_id,
            stability=request.stability,
            similarity_boost=request.similarity_boost,
            style=request.style,
            speed=request.speed,
        )
    except ElevenLabsIntegrationError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    headers = {
        "Cache-Control": "no-store",
        "X-Katcha-Voice-Id": voice_id,
        "X-Katcha-Model-Id": model_id,
    }
    if metadata.get("character_cost"):
        headers["X-Katcha-Character-Cost"] = str(metadata["character_cost"])
    return Response(content=audio, media_type="audio/mpeg", headers=headers)


@router.post(
    "/invideo/handoffs",
    response_model=InVideoHandoffResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_invideo_handoff(
    request: InVideoHandoffCreate,
) -> InVideoHandoffResponse:
    try:
        row = prepare_invideo_handoff(
            request.source_type,
            request.source_id,
            actor=request.actor,
            note=request.note,
        )
        return _response(row)
    except ValueError as exc:
        message = str(exc)
        code = 404 if "not found" in message else 409
        raise HTTPException(status_code=code, detail=message) from exc


@router.get("/invideo/handoffs", response_model=list[InVideoHandoffResponse])
def list_invideo_handoffs(
    source_type: Literal["production", "short_episode"] | None = Query(default=None),
    source_id: uuid.UUID | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=250),
) -> list[InVideoHandoffResponse]:
    with session_scope() as session:
        stmt = (
            select(ExternalEditHandoff)
            .where(ExternalEditHandoff.provider == "invideo")
            .order_by(ExternalEditHandoff.created_at.desc())
            .limit(limit)
        )
        if source_type is not None:
            stmt = stmt.where(ExternalEditHandoff.source_type == source_type)
        if source_id is not None:
            stmt = stmt.where(ExternalEditHandoff.source_id == source_id)
        return [_response(row) for row in session.scalars(stmt)]


@router.get(
    "/invideo/handoffs/{handoff_id}",
    response_model=InVideoHandoffResponse,
)
def get_invideo_handoff(handoff_id: uuid.UUID) -> InVideoHandoffResponse:
    with session_scope() as session:
        row = session.get(ExternalEditHandoff, handoff_id)
        if row is None:
            raise HTTPException(status_code=404, detail="external edit handoff not found")
        return _response(row)


@router.get("/invideo/handoffs/{handoff_id}/manifest")
def get_invideo_manifest(handoff_id: uuid.UUID) -> dict[str, object]:
    try:
        return handoff_manifest(handoff_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


def _cleanup_package(path: Path) -> None:
    shutil.rmtree(path.parent, ignore_errors=True)


@router.get("/invideo/handoffs/{handoff_id}/package")
def download_invideo_package(
    handoff_id: uuid.UUID,
    background_tasks: BackgroundTasks,
) -> FileResponse:
    try:
        archive = build_handoff_zip(handoff_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    background_tasks.add_task(_cleanup_package, archive)
    return FileResponse(
        path=archive,
        filename=archive.name,
        media_type="application/zip",
        background=background_tasks,
    )


@router.post(
    "/invideo/handoffs/{handoff_id}/output",
    response_model=InVideoHandoffResponse,
)
async def upload_invideo_output(
    handoff_id: uuid.UUID,
    file: Annotated[UploadFile, File()],
    external_project_id: Annotated[str | None, Form(max_length=255)] = None,
    actor: Annotated[str, Form(min_length=1, max_length=128)] = "operator",
) -> InVideoHandoffResponse:
    if file.content_type not in {None, "", "video/mp4", "application/octet-stream"}:
        raise HTTPException(status_code=415, detail="InVideo output must be an MP4 file")

    settings = get_settings()
    work_dir = settings.work_dir / "external-edit" / str(handoff_id)
    work_dir.mkdir(parents=True, exist_ok=True)
    target = work_dir / "output.mp4"
    size = 0
    max_bytes = 4 * 1024 * 1024 * 1024
    try:
        with target.open("wb") as handle:
            while chunk := await file.read(1024 * 1024):
                size += len(chunk)
                if size > max_bytes:
                    raise HTTPException(
                        status_code=413,
                        detail="external edit output must be 4 GB or smaller",
                    )
                handle.write(chunk)
        if size == 0:
            raise HTTPException(status_code=400, detail="external edit output is empty")
        row = import_external_output(
            handoff_id,
            target,
            external_project_id=external_project_id,
            actor=actor,
        )
        return _response(row)
    except ValueError as exc:
        message = str(exc)
        code = 404 if "not found" in message else 409
        raise HTTPException(status_code=code, detail=message) from exc
    finally:
        await file.close()
        shutil.rmtree(work_dir, ignore_errors=True)


@router.post(
    "/invideo/handoffs/{handoff_id}/adopt",
    response_model=InVideoHandoffResponse,
)
def adopt_invideo_output(
    handoff_id: uuid.UUID,
    request: InVideoHandoffAction,
) -> InVideoHandoffResponse:
    try:
        return _response(
            adopt_external_output(
                handoff_id,
                actor=request.actor,
            )
        )
    except ValueError as exc:
        message = str(exc)
        code = 404 if "not found" in message else 409
        raise HTTPException(status_code=code, detail=message) from exc
