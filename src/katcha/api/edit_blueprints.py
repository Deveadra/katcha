from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field

from katcha.edit_blueprint_models import ChannelEditBlueprintVersion
from katcha.services.channel_edit_blueprints import (
    activate_edit_blueprint_version,
    create_edit_blueprint_version,
    list_channel_edit_blueprints,
)

router = APIRouter(prefix="/v1/channels", tags=["channel-editing"])


class EditBlueprintResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    channel_profile_id: uuid.UUID
    blueprint_key: str
    version: int
    contract_version: str
    is_active: bool
    is_default: bool
    contract: dict[str, object]
    blueprint_metadata: dict[str, object]
    created_at: datetime


class CreateEditBlueprintRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    contract: dict[str, object]
    actor: str = Field(default="operator", min_length=1, max_length=128)
    set_default: bool = False


class ActivateEditBlueprintRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    actor: str = Field(default="operator", min_length=1, max_length=128)
    set_default: bool = False


@router.get(
    "/{channel_profile_id}/edit-blueprints",
    response_model=list[EditBlueprintResponse],
)
def get_edit_blueprints(
    channel_profile_id: uuid.UUID,
) -> list[ChannelEditBlueprintVersion]:
    try:
        return list_channel_edit_blueprints(channel_profile_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post(
    "/{channel_profile_id}/edit-blueprints",
    response_model=EditBlueprintResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_edit_blueprint(
    channel_profile_id: uuid.UUID,
    request: CreateEditBlueprintRequest,
) -> ChannelEditBlueprintVersion:
    try:
        return create_edit_blueprint_version(
            channel_profile_id,
            contract_payload=request.contract,
            actor=request.actor,
            set_default=request.set_default,
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post(
    "/{channel_profile_id}/edit-blueprints/{blueprint_key}/{version}/activate",
    response_model=EditBlueprintResponse,
)
def activate_edit_blueprint(
    channel_profile_id: uuid.UUID,
    blueprint_key: str,
    version: int,
    request: ActivateEditBlueprintRequest,
) -> ChannelEditBlueprintVersion:
    try:
        return activate_edit_blueprint_version(
            channel_profile_id,
            blueprint_key=blueprint_key,
            version=version,
            actor=request.actor,
            set_default=request.set_default,
        )
    except ValueError as exc:
        message = str(exc)
        code = 404 if "not found" in message else 409
        raise HTTPException(status_code=code, detail=message) from exc
