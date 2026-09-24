from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Literal

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field

from katcha.edit_blueprint_models import ChannelEditBlueprintVersion
from katcha.edit_performance_models import EditBlueprintPerformanceSnapshot
from katcha.services.channel_edit_blueprints import (
    activate_edit_blueprint_version,
    create_edit_blueprint_version,
    list_channel_edit_blueprints,
)
from katcha.services.edit_blueprint_performance import (
    latest_edit_blueprint_performance,
    list_edit_blueprint_performance,
    refresh_edit_blueprint_performance,
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



class EditBlueprintPerformanceResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    channel_profile_id: uuid.UUID
    version: int
    run_key: str
    age_bucket_hours: int
    publication_count: int
    blueprint_group_count: int
    revenue_covered_publications: int
    retention_covered_publications: int
    monetary_coverage: Decimal
    retention_coverage: Decimal
    aggregate_metrics: list[dict[str, object]]
    comparison_status: str
    comparison_summary: dict[str, object]
    sample_window_start: datetime | None
    sample_window_end: datetime | None
    created_at: datetime


class RefreshEditBlueprintPerformanceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_key: str | None = Field(default=None, min_length=1, max_length=160)
    age_bucket_hours: Literal[6, 24, 72, 168] = 72

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


@router.post(
    "/{channel_profile_id}/edit-blueprints/performance/refresh",
    response_model=EditBlueprintPerformanceResponse,
    status_code=status.HTTP_201_CREATED,
)
def refresh_edit_performance(
    channel_profile_id: uuid.UUID,
    request: RefreshEditBlueprintPerformanceRequest,
) -> EditBlueprintPerformanceSnapshot:
    run_key = request.run_key or f"manual-{uuid.uuid4().hex[:24]}"
    try:
        return refresh_edit_blueprint_performance(
            channel_profile_id,
            run_key=run_key,
            age_bucket_hours=request.age_bucket_hours,
        )
    except ValueError as exc:
        message = str(exc)
        code = 404 if "channel profile not found" in message else 409
        raise HTTPException(status_code=code, detail=message) from exc


@router.get(
    "/{channel_profile_id}/edit-blueprints/performance/latest",
    response_model=EditBlueprintPerformanceResponse | None,
)
def get_latest_edit_performance(
    channel_profile_id: uuid.UUID,
    age_bucket_hours: Literal[6, 24, 72, 168] | None = Query(default=None),
) -> EditBlueprintPerformanceSnapshot | None:
    return latest_edit_blueprint_performance(
        channel_profile_id,
        age_bucket_hours=age_bucket_hours,
    )


@router.get(
    "/{channel_profile_id}/edit-blueprints/performance",
    response_model=list[EditBlueprintPerformanceResponse],
)
def get_edit_performance_history(
    channel_profile_id: uuid.UUID,
    age_bucket_hours: Literal[6, 24, 72, 168] | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=250),
) -> list[EditBlueprintPerformanceSnapshot]:
    return list_edit_blueprint_performance(
        channel_profile_id,
        age_bucket_hours=age_bucket_hours,
        limit=limit,
    )
