from __future__ import annotations

import hashlib
import uuid
from datetime import datetime

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel, Field

from katcha.orchestration.client import start_trend_refresh_workflow
from katcha.services.trend_bridge import (
    TrendBridgeResult,
    bridge_discovery_run,
    bridge_topic_watch_queue,
    bridged_signals,
)
from katcha.services.trends import active_watch_profile
from katcha.trend_models import TrendSignal

router = APIRouter(prefix="/trends/bridge", tags=["trend-bridge"])


class TrendBridgeRequest(BaseModel):
    channel_profile_ids: list[uuid.UUID] = Field(default_factory=list, max_length=100)


class TrendBridgeRefreshResponse(BaseModel):
    channel_profile_id: uuid.UUID
    workflow_id: str
    run_key: str


class TrendBridgeResponse(BaseModel):
    scope: str
    signal_count: int
    observation_count: int
    candidate_count: int
    topic_count: int
    signal_ids: list[uuid.UUID]
    refreshed_channels: list[uuid.UUID]
    refreshes: list[TrendBridgeRefreshResponse]


class BridgedTrendSignalResponse(BaseModel):
    id: uuid.UUID
    provider_key: str
    external_id: str
    observation_key: str
    source_kind: str
    independence_key: str
    canonical_url: str | None
    observed_at: datetime
    published_at: datetime | None
    metrics: dict[str, float]
    media_refs: list[dict[str, object]]
    signal_metadata: dict[str, object]


def _unique_channel_ids(values: list[uuid.UUID]) -> list[uuid.UUID]:
    return list(dict.fromkeys(values))


def _refresh_identity(scope: str, channel_profile_id: uuid.UUID) -> tuple[str, str]:
    digest = hashlib.sha256(f"{scope}:{channel_profile_id}".encode()).hexdigest()[:20]
    run_key = f"discovery-bridge-{digest}"
    workflow_id = f"channel-trend-refresh-{channel_profile_id}-{digest}"
    return run_key, workflow_id


def _validate_refresh_channels(channel_profile_ids: list[uuid.UUID]) -> list[uuid.UUID]:
    unique = _unique_channel_ids(channel_profile_ids)
    missing = [
        channel_id
        for channel_id in unique
        if active_watch_profile(channel_id) is None
    ]
    if missing:
        joined = ", ".join(str(channel_id) for channel_id in missing)
        raise HTTPException(
            status_code=409,
            detail=f"trend watch profile is not configured for channel(s): {joined}",
        )
    return unique


async def _start_refreshes(
    scope: str,
    channel_profile_ids: list[uuid.UUID],
) -> list[TrendBridgeRefreshResponse]:
    refreshes: list[TrendBridgeRefreshResponse] = []
    for channel_id in channel_profile_ids:
        run_key, workflow_id = _refresh_identity(scope, channel_id)
        started_id = await start_trend_refresh_workflow(
            str(channel_id),
            workflow_id,
            run_key,
        )
        refreshes.append(
            TrendBridgeRefreshResponse(
                channel_profile_id=channel_id,
                workflow_id=started_id,
                run_key=run_key,
            )
        )
    return refreshes


def _response(
    result: TrendBridgeResult,
    refreshes: list[TrendBridgeRefreshResponse],
) -> TrendBridgeResponse:
    channels = [item.channel_profile_id for item in refreshes]
    return TrendBridgeResponse(
        scope=result.scope,
        signal_count=result.signal_count,
        observation_count=result.observation_count,
        candidate_count=result.candidate_count,
        topic_count=result.topic_count,
        signal_ids=list(result.signal_ids),
        refreshed_channels=channels,
        refreshes=refreshes,
    )


def _signal_response(signal: TrendSignal) -> BridgedTrendSignalResponse:
    return BridgedTrendSignalResponse(
        id=signal.id,
        provider_key=signal.provider_key,
        external_id=signal.external_id,
        observation_key=signal.observation_key,
        source_kind=signal.source_kind,
        independence_key=signal.independence_key,
        canonical_url=signal.canonical_url,
        observed_at=signal.observed_at,
        published_at=signal.published_at,
        metrics=dict(signal.metrics or {}),
        media_refs=list(signal.media_refs or []),
        signal_metadata=dict(signal.signal_metadata or {}),
    )


@router.post(
    "/runs/{discovery_run_id}",
    response_model=TrendBridgeResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def bridge_run(
    discovery_run_id: uuid.UUID,
    request: TrendBridgeRequest,
) -> TrendBridgeResponse:
    channels = _validate_refresh_channels(request.channel_profile_ids)
    try:
        result = bridge_discovery_run(discovery_run_id)
    except ValueError as exc:
        code = 404 if "not found" in str(exc) else 409
        raise HTTPException(status_code=code, detail=str(exc)) from exc
    refreshes = await _start_refreshes(result.scope, channels)
    return _response(result, refreshes)


@router.post(
    "/watches/{topic_watch_id}/queues/{queue_key}",
    response_model=TrendBridgeResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def bridge_queue(
    topic_watch_id: uuid.UUID,
    queue_key: str,
    request: TrendBridgeRequest,
) -> TrendBridgeResponse:
    channels = _validate_refresh_channels(request.channel_profile_ids)
    try:
        result = bridge_topic_watch_queue(
            topic_watch_id,
            queue_key=queue_key,
        )
    except ValueError as exc:
        code = 404 if "not found" in str(exc) else 409
        raise HTTPException(status_code=code, detail=str(exc)) from exc
    refreshes = await _start_refreshes(result.scope, channels)
    return _response(result, refreshes)


@router.get(
    "/signals",
    response_model=list[BridgedTrendSignalResponse],
)
def inspect_bridged_signals(
    discovery_run_id: uuid.UUID | None = Query(default=None),
    discovery_candidate_id: uuid.UUID | None = Query(default=None),
    queue_key: str | None = Query(default=None, max_length=160),
    limit: int = Query(default=100, ge=1, le=500),
) -> list[BridgedTrendSignalResponse]:
    try:
        signals = bridged_signals(
            discovery_run_id=discovery_run_id,
            discovery_candidate_id=discovery_candidate_id,
            queue_key=queue_key,
            limit=limit,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return [_signal_response(signal) for signal in signals]
