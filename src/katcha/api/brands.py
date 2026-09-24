from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field

from katcha.brand_models import ChannelBrandVersion
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
