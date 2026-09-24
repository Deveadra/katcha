from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field

from katcha.brand_models import ChannelBrandVersion
from katcha.brand_preview_models import BrandPreviewRender
from katcha.orchestration.client import start_brand_preview_workflow
from katcha.services.brand_previews import get_brand_preview, register_brand_preview
from katcha.services.channel_brands import (
    activate_brand_version,
    builtin_brand_candidates,
    list_channel_brand_versions,
    stage_brand_version,
)

router = APIRouter(prefix="/v1/channels", tags=["channel-branding"])


class BrandVersionResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    channel_profile_id: uuid.UUID
    version: int
    brand_key: str
    is_active: bool
    contract: dict[str, object]
    brand_metadata: dict[str, object]
    created_at: datetime


class BrandCandidateResponse(BaseModel):
    brand_key: str
    version: int
    contract: dict[str, object]


class StageBrandVersionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    contract: dict[str, object]
    actor: str = Field(default="operator", min_length=1, max_length=128)
    hypothesis: str | None = Field(default=None, max_length=1000)


class ActivateBrandVersionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    actor: str = Field(default="operator", min_length=1, max_length=128)


class PreviewReactionCueRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=128)
    asset_key: str = Field(min_length=1, max_length=128)
    line_ref: int = Field(ge=0)
    offset_seconds: float = Field(default=0, ge=0)
    duration_seconds: float = Field(default=1.2, ge=0.2, le=5)
    anchor: str = "bottom_right"
    animation: str = "pop_bounce"
    scale: float = Field(default=0.22, ge=0.08, le=0.38)


class CreateBrandPreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    production_id: uuid.UUID
    reaction_cue: PreviewReactionCueRequest | None = None


class BrandPreviewResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    channel_profile_id: uuid.UUID
    production_id: uuid.UUID
    brand_version_id: uuid.UUID
    brand_key: str
    brand_version: int
    workflow_id: str
    status: str
    source_lineage: dict[str, object]
    brand_snapshot: dict[str, object]
    reaction_cue: dict[str, object] | None
    render_manifest: dict[str, object]
    output_key: str | None
    verification: dict[str, object]
    error: str | None
    created_at: datetime
    updated_at: datetime


@router.get(
    "/{channel_profile_id}/brands",
    response_model=list[BrandVersionResponse],
)
def list_brands(
    channel_profile_id: uuid.UUID,
) -> list[ChannelBrandVersion]:
    try:
        return list_channel_brand_versions(channel_profile_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get(
    "/{channel_profile_id}/brand-candidates",
    response_model=list[BrandCandidateResponse],
)
def list_brand_candidates(
    channel_profile_id: uuid.UUID,
) -> list[BrandCandidateResponse]:
    try:
        return [
            BrandCandidateResponse(
                brand_key=contract.brand_key,
                version=contract.version,
                contract=contract.model_dump(mode="json"),
            )
            for contract in builtin_brand_candidates(channel_profile_id)
        ]
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post(
    "/{channel_profile_id}/brands",
    response_model=BrandVersionResponse,
    status_code=status.HTTP_201_CREATED,
)
def stage_brand(
    channel_profile_id: uuid.UUID,
    request: StageBrandVersionRequest,
) -> ChannelBrandVersion:
    try:
        return stage_brand_version(
            channel_profile_id,
            contract_payload=request.contract,
            actor=request.actor,
            hypothesis=request.hypothesis,
        )
    except ValueError as exc:
        message = str(exc)
        code = 404 if "channel profile not found" in message else 409
        raise HTTPException(status_code=code, detail=message) from exc


@router.post(
    "/{channel_profile_id}/brands/{version}/previews",
    response_model=BrandPreviewResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def create_brand_preview(
    channel_profile_id: uuid.UUID,
    version: int,
    request: CreateBrandPreviewRequest,
) -> BrandPreviewRender:
    try:
        row = register_brand_preview(
            channel_profile_id,
            production_id=request.production_id,
            brand_version=version,
            reaction_cue=(
                request.reaction_cue.model_dump(mode="json")
                if request.reaction_cue is not None
                else None
            ),
        )
    except ValueError as exc:
        message = str(exc)
        code = 404 if "not found" in message else 409
        raise HTTPException(status_code=code, detail=message) from exc
    if row.status == "queued":
        await start_brand_preview_workflow(str(row.id), row.workflow_id)
    return row


@router.get(
    "/{channel_profile_id}/brand-previews/{preview_id}",
    response_model=BrandPreviewResponse,
)
def get_staged_brand_preview(
    channel_profile_id: uuid.UUID,
    preview_id: uuid.UUID,
) -> BrandPreviewRender:
    try:
        return get_brand_preview(channel_profile_id, preview_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post(
    "/{channel_profile_id}/brands/{version}/activate",
    response_model=BrandVersionResponse,
)
def activate_brand(
    channel_profile_id: uuid.UUID,
    version: int,
    request: ActivateBrandVersionRequest,
) -> ChannelBrandVersion:
    try:
        return activate_brand_version(
            channel_profile_id,
            version=version,
            actor=request.actor,
        )
    except ValueError as exc:
        message = str(exc)
        code = 404 if "not found" in message else 409
        raise HTTPException(status_code=code, detail=message) from exc
