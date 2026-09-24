from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field

from katcha.edit_render_models import RenderAttempt
from katcha.orchestration.client import (
    start_production_workflow,
    start_short_episode_editorial_workflow,
)
from katcha.services.render_recovery import (
    list_render_attempts,
    reserve_render_retry,
)

router = APIRouter(tags=["render-recovery"])


class RenderAttemptResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    production_id: uuid.UUID | None
    short_episode_id: uuid.UUID | None
    retry_of_id: uuid.UUID | None
    attempt_number: int
    workflow_id: str
    status: str
    requested_by: str
    manifest_version: str | None
    manifest_sha256: str | None
    output_key: str | None
    pre_qc: dict[str, object]
    post_qc: dict[str, object]
    failure_kind: str | None
    error: str | None
    attempt_metadata: dict[str, object]
    started_at: datetime | None
    completed_at: datetime | None
    created_at: datetime
    updated_at: datetime


class RetryRenderRequest(BaseModel):
    actor: str = Field(default="operator", min_length=1, max_length=128)


class RetryRenderResponse(BaseModel):
    source_kind: Literal["production", "short_episode"]
    source_id: uuid.UUID
    render_attempt_id: uuid.UUID
    attempt_number: int
    workflow_id: str


def _history(
    source_kind: Literal["production", "short_episode"],
    source_id: uuid.UUID,
) -> list[RenderAttempt]:
    try:
        return list_render_attempts(source_kind, source_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get(
    "/v1/productions/{production_id}/render-attempts",
    response_model=list[RenderAttemptResponse],
)
def production_render_attempts(production_id: uuid.UUID) -> list[RenderAttempt]:
    return _history("production", production_id)


@router.post(
    "/v1/productions/{production_id}/render-retry",
    response_model=RetryRenderResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def retry_production_render(
    production_id: uuid.UUID,
    request: RetryRenderRequest,
) -> RetryRenderResponse:
    try:
        attempt = reserve_render_retry(
            "production",
            production_id,
            actor=request.actor,
        )
    except ValueError as exc:
        message = str(exc)
        code = 404 if "not found" in message else 409
        raise HTTPException(status_code=code, detail=message) from exc
    await start_production_workflow(
        str(production_id),
        attempt.workflow_id,
        start_stage="render",
    )
    return RetryRenderResponse(
        source_kind="production",
        source_id=production_id,
        render_attempt_id=attempt.id,
        attempt_number=attempt.attempt_number,
        workflow_id=attempt.workflow_id,
    )


@router.get(
    "/v1/short-episodes/{short_episode_id}/render-attempts",
    response_model=list[RenderAttemptResponse],
)
def short_episode_render_attempts(
    short_episode_id: uuid.UUID,
) -> list[RenderAttempt]:
    return _history("short_episode", short_episode_id)


@router.post(
    "/v1/short-episodes/{short_episode_id}/render-retry",
    response_model=RetryRenderResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def retry_short_episode_render(
    short_episode_id: uuid.UUID,
    request: RetryRenderRequest,
) -> RetryRenderResponse:
    try:
        attempt = reserve_render_retry(
            "short_episode",
            short_episode_id,
            actor=request.actor,
        )
    except ValueError as exc:
        message = str(exc)
        code = 404 if "not found" in message else 409
        raise HTTPException(status_code=code, detail=message) from exc
    await start_short_episode_editorial_workflow(
        str(short_episode_id),
        attempt.workflow_id,
        start_stage="render",
    )
    return RetryRenderResponse(
        source_kind="short_episode",
        source_id=short_episode_id,
        render_attempt_id=attempt.id,
        attempt_number=attempt.attempt_number,
        workflow_id=attempt.workflow_id,
    )
