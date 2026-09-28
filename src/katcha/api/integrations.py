from __future__ import annotations

import shutil
import uuid
from pathlib import Path
from typing import Literal

from fastapi import (
    APIRouter,
    BackgroundTasks,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    UploadFile,
    status,
)
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from katcha.api.control_auth import control_actor, require_control_scope
from katcha.config import get_settings
from katcha.db import session_scope
from katcha.external_edit_models import ExternalEditHandoff
from katcha.services.external_edit import (
    adopt_external_output,
    build_handoff_zip,
    handoff_manifest,
    import_external_output,
    prepare_invideo_handoff,
)

router = APIRouter(prefix="/v1/integrations", tags=["integrations"])


class IntegrationProviderStatus(BaseModel):
    provider: str
    capability: str
    configured: bool
    mode: str
    detail: str


class InVideoHandoffCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_type: Literal["production", "short_episode"]
    source_id: uuid.UUID
    note: str | None = Field(default=None, max_length=2000)


class InVideoHandoffAction(BaseModel):
    model_config = ConfigDict(extra="forbid")


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
def integration_provider_status(
    http_request: Request,
) -> list[IntegrationProviderStatus]:
    require_control_scope(http_request, "integrations:read")
    settings = get_settings()
    elevenlabs_configured = bool(
        settings.elevenlabs_api_key and settings.elevenlabs_voice_id
    )
    return [
        IntegrationProviderStatus(
            provider="elevenlabs",
            capability="text_to_speech",
            configured=elevenlabs_configured,
            mode="api",
            detail=(
                "Direct ElevenLabs TTS is ready."
                if elevenlabs_configured
                else "Set KATCHA_ELEVENLABS_API_KEY and KATCHA_ELEVENLABS_VOICE_ID."
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


@router.post(
    "/invideo/handoffs",
    response_model=InVideoHandoffResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_invideo_handoff(
    request: InVideoHandoffCreate,
    http_request: Request,
) -> InVideoHandoffResponse:
    require_control_scope(http_request, "integrations:write")
    actor = control_actor(http_request)
    try:
        row = prepare_invideo_handoff(
            request.source_type,
            request.source_id,
            actor=actor,
            note=request.note,
        )
        return _response(row)
    except ValueError as exc:
        message = str(exc)
        code = 404 if "not found" in message else 409
        raise HTTPException(status_code=code, detail=message) from exc


@router.get("/invideo/handoffs", response_model=list[InVideoHandoffResponse])
def list_invideo_handoffs(
    http_request: Request,
    source_type: Literal["production", "short_episode"] | None = Query(default=None),
    source_id: uuid.UUID | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=250),
) -> list[InVideoHandoffResponse]:
    require_control_scope(http_request, "integrations:read")
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
def get_invideo_handoff(
    handoff_id: uuid.UUID,
    http_request: Request,
) -> InVideoHandoffResponse:
    require_control_scope(http_request, "integrations:read")
    with session_scope() as session:
        row = session.get(ExternalEditHandoff, handoff_id)
        if row is None:
            raise HTTPException(status_code=404, detail="external edit handoff not found")
        return _response(row)


@router.get("/invideo/handoffs/{handoff_id}/manifest")
def get_invideo_manifest(
    handoff_id: uuid.UUID,
    http_request: Request,
) -> dict[str, object]:
    require_control_scope(http_request, "integrations:read")
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
    http_request: Request,
) -> FileResponse:
    require_control_scope(http_request, "integrations:read")
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
    http_request: Request,
    file: UploadFile = File(...),
    external_project_id: str | None = Form(default=None, max_length=255),
) -> InVideoHandoffResponse:
    require_control_scope(http_request, "integrations:write")
    actor = control_actor(http_request)
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
    http_request: Request,
) -> InVideoHandoffResponse:
    require_control_scope(http_request, "integrations:write")
    actor = control_actor(http_request)
    try:
        return _response(
            adopt_external_output(
                handoff_id,
                actor=actor,
            )
        )
    except ValueError as exc:
        message = str(exc)
        code = 404 if "not found" in message else 409
        raise HTTPException(status_code=code, detail=message) from exc
