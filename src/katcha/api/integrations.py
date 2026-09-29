from __future__ import annotations

import shutil
import uuid
from pathlib import Path
from typing import Annotated, Literal

import httpx
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
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from katcha.audio.tts import get_voice_profile, synthesize_speech
from katcha.config import get_settings
from katcha.db import session_scope
from katcha.external_edit_models import ExternalEditHandoff
from katcha.services.channel_brands import (
    activate_brand_version,
    brand_for_channel,
    stage_brand_version,
)
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


class ElevenLabsVerificationResponse(BaseModel):
    configured: bool
    reachable: bool
    voice_id_hint: str | None
    model_id: str
    output_format: str
    live_execution: bool
    detail: str


class ElevenLabsPreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    channel_profile_id: uuid.UUID
    text: str = Field(min_length=1, max_length=400)


class ChannelVoicePolicyResponse(BaseModel):
    channel_profile_id: uuid.UUID
    brand_version: int
    routing_mode: Literal["inherit", "fixed"]
    preferred_profiles: list[str]
    primary_provider: str
    fallback_providers: list[str]


class ChannelVoicePolicyUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    primary_provider: Literal["elevenlabs", "openai", "gemini"]
    fallback_providers: list[Literal["elevenlabs", "openai", "gemini"]] = Field(
        default_factory=list,
        max_length=2,
    )
    routing_mode: Literal["inherit", "fixed"] = "fixed"
    actor: str = Field(default="channel-studio", min_length=1, max_length=128)


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


_VOICE_PROFILE_BY_PROVIDER = {
    "elevenlabs": "elevenlabs_rank_snaxx_v1",
    "openai": "openai_youth_v2",
    "gemini": "gemini_youth_v2",
}


def _provider_configured(provider: str) -> bool:
    settings = get_settings()
    if provider == "elevenlabs":
        return bool(settings.elevenlabs_api_key and settings.elevenlabs_voice_id)
    if provider == "openai":
        return bool(settings.openai_api_key)
    if provider == "gemini":
        return bool(settings.gemini_api_key)
    return False


def _provider_for_profile(profile_key: str) -> str:
    for provider, key in _VOICE_PROFILE_BY_PROVIDER.items():
        if key == profile_key:
            return provider
    return "unknown"


def _voice_policy_response(
    channel_profile_id: uuid.UUID,
    *,
    brand_version: int,
    voice_policy: dict[str, object],
) -> ChannelVoicePolicyResponse:
    profiles = [str(value) for value in voice_policy.get("preferred_profiles") or []]
    providers = [_provider_for_profile(value) for value in profiles]
    primary = providers[0] if providers else "unknown"
    routing_mode: Literal["inherit", "fixed"] = (
        "fixed"
        if str(voice_policy.get("routing_mode") or "inherit") == "fixed"
        else "inherit"
    )
    return ChannelVoicePolicyResponse(
        channel_profile_id=channel_profile_id,
        brand_version=brand_version,
        routing_mode=routing_mode,
        preferred_profiles=profiles,
        primary_provider=primary,
        fallback_providers=[value for value in providers[1:] if value != "unknown"],
    )


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


@router.get(
    "/elevenlabs/verify",
    response_model=ElevenLabsVerificationResponse,
)
def verify_elevenlabs() -> ElevenLabsVerificationResponse:
    settings = get_settings()
    api_key = settings.elevenlabs_api_key
    voice_id = settings.elevenlabs_voice_id
    hint = None
    if voice_id:
        hint = f"{voice_id[:4]}…{voice_id[-4:]}" if len(voice_id) > 10 else "configured"
    if not api_key or not voice_id:
        return ElevenLabsVerificationResponse(
            configured=False,
            reachable=False,
            voice_id_hint=hint,
            model_id=settings.elevenlabs_model_id,
            output_format=settings.elevenlabs_output_format,
            live_execution=settings.resolved_ai_execution_mode() == "live",
            detail="ElevenLabs API key and voice ID are not both configured.",
        )
    try:
        response = httpx.get(
            f"https://api.elevenlabs.io/v1/voices/{voice_id}/settings",
            headers={"xi-api-key": api_key},
            timeout=settings.elevenlabs_timeout_seconds,
        )
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        return ElevenLabsVerificationResponse(
            configured=True,
            reachable=False,
            voice_id_hint=hint,
            model_id=settings.elevenlabs_model_id,
            output_format=settings.elevenlabs_output_format,
            live_execution=settings.resolved_ai_execution_mode() == "live",
            detail=(
                "ElevenLabs rejected the configured voice check "
                f"(HTTP {exc.response.status_code})."
            ),
        )
    except httpx.HTTPError:
        return ElevenLabsVerificationResponse(
            configured=True,
            reachable=False,
            voice_id_hint=hint,
            model_id=settings.elevenlabs_model_id,
            output_format=settings.elevenlabs_output_format,
            live_execution=settings.resolved_ai_execution_mode() == "live",
            detail="ElevenLabs could not be reached from the Katcha API.",
        )
    return ElevenLabsVerificationResponse(
        configured=True,
        reachable=True,
        voice_id_hint=hint,
        model_id=settings.elevenlabs_model_id,
        output_format=settings.elevenlabs_output_format,
        live_execution=settings.resolved_ai_execution_mode() == "live",
        detail="Configured ElevenLabs credentials and voice are reachable.",
    )


@router.post("/elevenlabs/preview")
def preview_elevenlabs_voice(request: ElevenLabsPreviewRequest) -> StreamingResponse:
    settings = get_settings()
    if not settings.elevenlabs_api_key or not settings.elevenlabs_voice_id:
        raise HTTPException(status_code=409, detail="ElevenLabs is not configured")
    if settings.resolved_ai_execution_mode() != "live":
        raise HTTPException(
            status_code=409,
            detail=(
                "Voice preview is a real paid provider call. "
                "Set KATCHA_AI_EXECUTION_MODE=live before previewing ElevenLabs."
            ),
        )
    profile = get_voice_profile("elevenlabs_rank_snaxx_v1")
    result = synthesize_speech(
        request.text,
        profile=profile,
        settings=settings,
        channel_profile_id=request.channel_profile_id,
        reference_type="voice_preview",
        reference_id=str(request.channel_profile_id),
        reservation_key=(
            f"voice-preview:{request.channel_profile_id}:{uuid.uuid4().hex}"
        ),
        expected_value=0.5,
        usage_metadata={"surface": "channel_studio"},
    )
    return StreamingResponse(
        iter([result.audio]),
        media_type=result.content_type,
        headers={
            "Content-Disposition": 'inline; filename="elevenlabs-preview.wav"',
            "X-Katcha-Voice-Profile": result.profile.key,
            "X-Katcha-TTS-Provider": result.target.provider,
            "X-Katcha-Estimated-Cost-USD": str(result.estimated_cost_usd),
        },
    )


@router.get(
    "/channels/{channel_profile_id}/voice-policy",
    response_model=ChannelVoicePolicyResponse,
)
def get_channel_voice_policy(
    channel_profile_id: uuid.UUID,
) -> ChannelVoicePolicyResponse:
    try:
        with session_scope() as session:
            contract, version = brand_for_channel(session, channel_profile_id)
        return _voice_policy_response(
            channel_profile_id,
            brand_version=version,
            voice_policy=contract.voice_policy.model_dump(mode="json"),
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.put(
    "/channels/{channel_profile_id}/voice-policy",
    response_model=ChannelVoicePolicyResponse,
)
def update_channel_voice_policy(
    channel_profile_id: uuid.UUID,
    request: ChannelVoicePolicyUpdate,
) -> ChannelVoicePolicyResponse:
    providers = [request.primary_provider, *request.fallback_providers]
    if len(providers) != len(set(providers)):
        raise HTTPException(status_code=400, detail="voice providers cannot be repeated")
    unavailable = [provider for provider in providers if not _provider_configured(provider)]
    if unavailable:
        raise HTTPException(
            status_code=409,
            detail=(
                "voice provider is not configured: "
                + ", ".join(unavailable)
            ),
        )
    try:
        with session_scope() as session:
            contract, version = brand_for_channel(session, channel_profile_id)
        payload = contract.model_dump(mode="json")
        payload["version"] = version + 1
        voice_policy = dict(payload.get("voice_policy") or {})
        voice_policy["preferred_profiles"] = [
            _VOICE_PROFILE_BY_PROVIDER[provider] for provider in providers
        ]
        voice_policy["routing_mode"] = request.routing_mode
        payload["voice_policy"] = voice_policy
        staged = stage_brand_version(
            channel_profile_id,
            contract_payload=payload,
            actor=request.actor,
            hypothesis=(
                f"Set {request.primary_provider} as the channel narration provider "
                f"with {request.routing_mode} routing."
            ),
        )
        active = activate_brand_version(
            channel_profile_id,
            version=staged.version,
            actor=request.actor,
        )
        return _voice_policy_response(
            channel_profile_id,
            brand_version=active.version,
            voice_policy=dict(active.contract.get("voice_policy") or {}),
        )
    except ValueError as exc:
        message = str(exc)
        code = 404 if "not found" in message else 409
        raise HTTPException(status_code=code, detail=message) from exc


@router.get("/providers", response_model=list[IntegrationProviderStatus])
def integration_provider_status() -> list[IntegrationProviderStatus]:
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
            provider="openai",
            capability="text_to_speech",
            configured=bool(settings.openai_api_key),
            mode="api",
            detail=(
                "OpenAI TTS is available as a channel voice or fallback."
                if settings.openai_api_key
                else "OpenAI TTS is not configured."
            ),
        ),
        IntegrationProviderStatus(
            provider="gemini",
            capability="text_to_speech",
            configured=bool(settings.gemini_api_key),
            mode="api",
            detail=(
                "Gemini TTS is available as a channel voice or fallback."
                if settings.gemini_api_key
                else "Gemini TTS is not configured."
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
