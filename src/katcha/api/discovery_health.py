from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from katcha.discovery_health_models import DiscoveryPollAttempt, DiscoverySourceState
from katcha.services.discovery_health import (
    get_source_state,
    list_poll_attempts,
    list_source_states,
    reset_source_state,
    source_health_summary,
)

router = APIRouter(prefix="/trends/discovery/sources", tags=["discovery-health"])


class DiscoverySourceStateResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    source_key: str
    adapter_key: str
    adapter_version: str
    query_fingerprint: str
    effective_query: dict[str, object]
    health_status: str
    consecutive_failures: int
    total_polls: int
    total_successes: int
    total_empty_successes: int
    total_failures: int
    total_rate_limits: int
    cursor: dict[str, object]
    last_outcome: str | None
    last_error_kind: str | None
    last_poll_at: datetime | None
    last_success_at: datetime | None
    last_failure_at: datetime | None
    next_eligible_poll_at: datetime | None
    rate_limit_reset_at: datetime | None
    quota_limit_per_day: int | None
    quota_used: int
    quota_window_started_at: datetime | None
    state_metadata: dict[str, object]
    created_at: datetime
    updated_at: datetime


class DiscoveryPollAttemptResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    source_state_id: uuid.UUID
    discovery_run_id: uuid.UUID | None
    attempt_key: str
    outcome: str
    candidate_count: int
    pages: int
    cursor_before: dict[str, object]
    cursor_after: dict[str, object]
    http_status: int | None
    retry_after_seconds: int | None
    error_kind: str | None
    attempt_metadata: dict[str, object]
    started_at: datetime
    completed_at: datetime | None


class DiscoverySourceResetRequest(BaseModel):
    actor: str = Field(default="operator", min_length=1, max_length=128)
    reset_quota: bool = False


class DiscoveryHealthSummaryResponse(BaseModel):
    counts: dict[str, int]


@router.get("/summary", response_model=DiscoveryHealthSummaryResponse)
def get_discovery_health_summary(
    topic_watch_id: uuid.UUID | None = Query(default=None),
) -> DiscoveryHealthSummaryResponse:
    return DiscoveryHealthSummaryResponse(
        counts=source_health_summary(topic_watch_id=topic_watch_id)
    )


@router.get("", response_model=list[DiscoverySourceStateResponse])
def get_discovery_sources(
    topic_watch_id: uuid.UUID | None = Query(default=None),
    health_status: str | None = Query(default=None, max_length=32),
    limit: int = Query(default=100, ge=1, le=500),
) -> list[DiscoverySourceState]:
    return list_source_states(
        topic_watch_id=topic_watch_id,
        health_status=health_status,
        limit=limit,
    )


@router.get("/{source_key}", response_model=DiscoverySourceStateResponse)
def get_discovery_source(source_key: str) -> DiscoverySourceState:
    state = get_source_state(source_key)
    if state is None:
        raise HTTPException(status_code=404, detail="discovery source not found")
    return state


@router.get(
    "/{source_key}/polls",
    response_model=list[DiscoveryPollAttemptResponse],
)
def get_discovery_source_polls(
    source_key: str,
    limit: int = Query(default=100, ge=1, le=500),
) -> list[DiscoveryPollAttempt]:
    state = get_source_state(source_key)
    if state is None:
        raise HTTPException(status_code=404, detail="discovery source not found")
    return list_poll_attempts(state.id, limit=limit)


@router.post("/{source_key}/reset", response_model=DiscoverySourceStateResponse)
def reset_discovery_source(
    source_key: str,
    request: DiscoverySourceResetRequest,
) -> DiscoverySourceState:
    try:
        return reset_source_state(
            source_key,
            actor=request.actor,
            reset_quota=request.reset_quota,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
