from __future__ import annotations

import uuid

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from katcha.services.trend_bridge import (
    TrendBridgeResult,
    bridge_discovery_run,
    bridge_topic_watch_queue,
)

router = APIRouter(prefix="/trends/bridge", tags=["trend-bridge"])


class TrendBridgeRequest(BaseModel):
    channel_profile_ids: list[uuid.UUID] = Field(default_factory=list, max_length=100)


class TrendBridgeResponse(BaseModel):
    scope: str
    signal_count: int
    observation_count: int
    candidate_count: int
    topic_count: int
    signal_ids: list[uuid.UUID]
    refreshed_channels: list[uuid.UUID]


def _response(result: TrendBridgeResult) -> TrendBridgeResponse:
    return TrendBridgeResponse(
        scope=result.scope,
        signal_count=result.signal_count,
        observation_count=result.observation_count,
        candidate_count=result.candidate_count,
        topic_count=result.topic_count,
        signal_ids=list(result.signal_ids),
        refreshed_channels=list(result.refreshed_channels),
    )


@router.post(
    "/runs/{discovery_run_id}",
    response_model=TrendBridgeResponse,
)
def bridge_run(
    discovery_run_id: uuid.UUID,
    request: TrendBridgeRequest,
) -> TrendBridgeResponse:
    try:
        result = bridge_discovery_run(
            discovery_run_id,
            channel_profile_ids=request.channel_profile_ids,
        )
    except ValueError as exc:
        code = 404 if "not found" in str(exc) else 409
        raise HTTPException(status_code=code, detail=str(exc)) from exc
    return _response(result)


@router.post(
    "/watches/{topic_watch_id}/queues/{queue_key}",
    response_model=TrendBridgeResponse,
)
def bridge_queue(
    topic_watch_id: uuid.UUID,
    queue_key: str,
    request: TrendBridgeRequest,
) -> TrendBridgeResponse:
    try:
        result = bridge_topic_watch_queue(
            topic_watch_id,
            queue_key=queue_key,
            channel_profile_ids=request.channel_profile_ids,
        )
    except ValueError as exc:
        code = 404 if "not found" in str(exc) else 409
        raise HTTPException(status_code=code, detail=str(exc)) from exc
    return _response(result)
