from __future__ import annotations

import hashlib
import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field

from katcha.orchestration.client import (
    start_trend_source_poll_workflow,
    start_trend_source_schedule,
)
from katcha.services.trend_sources import (
    available_source_adapters,
    create_trend_source_subscription,
    get_trend_source,
    list_trend_sources,
    set_trend_source_status,
    source_health_summary,
)
from katcha.trend_source_models import TrendSourceSubscription

router = APIRouter(tags=["trend-sources"])


class TrendSourceRequest(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    adapter_key: str = Field(min_length=1, max_length=64)
    adapter_version: str = Field(default="v1", min_length=1, max_length=32)
    query: dict[str, Any]
    poll_interval_seconds: int = Field(default=900, ge=60, le=86400)
    metadata: dict[str, Any] = Field(default_factory=dict)
    actor: str = Field(default="operator", min_length=1, max_length=128)
    start_schedule: bool = True


class TrendSourceResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    channel_profile_id: uuid.UUID
    subscription_key: str
    name: str
    adapter_key: str
    adapter_version: str
    status: str
    source_query: dict[str, Any]
    cursor: dict[str, Any]
    poll_interval_seconds: int
    health_status: str
    consecutive_failures: int
    last_success_at: datetime | None
    last_failure_at: datetime | None
    next_poll_at: datetime | None
    backoff_until: datetime | None
    last_error_kind: str | None
    last_error_summary: str | None
    source_metadata: dict[str, Any]
    created_at: datetime
    updated_at: datetime


class TrendSourceCreateResponse(BaseModel):
    source: TrendSourceResponse
    schedule_workflow_id: str | None = None


class TrendSourceStatusRequest(BaseModel):
    enabled: bool
    actor: str = Field(default="operator", min_length=1, max_length=128)


class TrendSourcePollRequest(BaseModel):
    idempotency_key: str | None = Field(default=None, max_length=256)


class TrendSourcePollResponse(BaseModel):
    trend_source_subscription_id: uuid.UUID
    workflow_id: str
    run_key: str


def _poll_identity(source_id: uuid.UUID, key: str | None) -> tuple[str, str]:
    run_key = key.strip() if key and key.strip() else f"manual-{uuid.uuid4().hex}"
    digest = hashlib.sha256(run_key.encode("utf-8")).hexdigest()[:20]
    return run_key, f"trend-source-poll-{source_id}-{digest}"


@router.get("/trends/adapters")
def list_trend_source_adapters() -> list[dict[str, str]]:
    return available_source_adapters()


@router.post(
    "/channels/{channel_profile_id}/trends/sources",
    response_model=TrendSourceCreateResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def create_trend_source(
    channel_profile_id: uuid.UUID,
    request: TrendSourceRequest,
) -> TrendSourceCreateResponse:
    try:
        source = create_trend_source_subscription(
            channel_profile_id,
            name=request.name,
            adapter_key=request.adapter_key,
            adapter_version=request.adapter_version,
            query=request.query,
            poll_interval_seconds=request.poll_interval_seconds,
            metadata=request.metadata,
            actor=request.actor,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    schedule_workflow_id = None
    if request.start_schedule:
        schedule_workflow_id = f"trend-source-schedule-{source.id}"
        await start_trend_source_schedule(
            str(source.id),
            schedule_workflow_id,
            interval_seconds=source.poll_interval_seconds,
        )
    return TrendSourceCreateResponse(
        source=TrendSourceResponse.model_validate(source),
        schedule_workflow_id=schedule_workflow_id,
    )


@router.get(
    "/channels/{channel_profile_id}/trends/sources",
    response_model=list[TrendSourceResponse],
)
def get_channel_trend_sources(
    channel_profile_id: uuid.UUID,
) -> list[TrendSourceSubscription]:
    return list_trend_sources(channel_profile_id)


@router.get("/channels/{channel_profile_id}/trends/sources/health")
def get_channel_trend_source_health(channel_profile_id: uuid.UUID) -> dict[str, Any]:
    return source_health_summary(channel_profile_id)


@router.get(
    "/trends/sources/{source_id}",
    response_model=TrendSourceResponse,
)
def get_trend_source_detail(source_id: uuid.UUID) -> TrendSourceSubscription:
    source = get_trend_source(source_id)
    if source is None:
        raise HTTPException(status_code=404, detail="trend source not found")
    return source


@router.post(
    "/trends/sources/{source_id}/status",
    response_model=TrendSourceResponse,
)
def update_trend_source_status(
    source_id: uuid.UUID,
    request: TrendSourceStatusRequest,
) -> TrendSourceSubscription:
    try:
        return set_trend_source_status(
            source_id,
            enabled=request.enabled,
            actor=request.actor,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post(
    "/trends/sources/{source_id}/poll",
    response_model=TrendSourcePollResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def poll_trend_source_now(
    source_id: uuid.UUID,
    request: TrendSourcePollRequest,
) -> TrendSourcePollResponse:
    if get_trend_source(source_id) is None:
        raise HTTPException(status_code=404, detail="trend source not found")
    run_key, workflow_id = _poll_identity(source_id, request.idempotency_key)
    await start_trend_source_poll_workflow(str(source_id), workflow_id, run_key)
    return TrendSourcePollResponse(
        trend_source_subscription_id=source_id,
        workflow_id=workflow_id,
        run_key=run_key,
    )
