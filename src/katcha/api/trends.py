from __future__ import annotations

import hashlib
import uuid
from datetime import datetime
from decimal import Decimal

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field

from katcha.orchestration.client import start_trend_refresh_workflow
from katcha.services.trends import (
    active_watch_profile,
    create_watch_profile,
    latest_evidence_packet,
    list_opportunities,
    register_signal,
)
from katcha.trend_models import (
    ChannelTrendWatchVersion,
    TrendEvidencePacket,
    TrendOpportunity,
    TrendSignal,
)

router = APIRouter(prefix="/v1", tags=["trend-intelligence"])


class TrendWatchRequest(BaseModel):
    interests: list[str] = Field(min_length=1, max_length=100)
    excluded_terms: list[str] = Field(default_factory=list, max_length=100)
    entities: list[str] = Field(default_factory=list, max_length=100)
    platforms: list[str] = Field(default_factory=list, max_length=50)
    languages: list[str] = Field(default_factory=list, max_length=50)
    regions: list[str] = Field(default_factory=list, max_length=50)
    source_weights: dict[str, float] = Field(default_factory=dict)
    freshness_horizon_hours: int = Field(default=72, ge=6, le=720)
    min_confidence: float = Field(default=0.45, ge=0, le=1)
    opportunity_threshold: float = Field(default=0.55, ge=0, le=1)
    metadata: dict[str, object] = Field(default_factory=dict)
    actor: str = Field(default="operator", min_length=1, max_length=128)


class TrendWatchResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    channel_profile_id: uuid.UUID
    version: int
    interests: list[str]
    excluded_terms: list[str]
    entities: list[str]
    platforms: list[str]
    languages: list[str]
    regions: list[str]
    source_weights: dict[str, float]
    freshness_horizon_hours: int
    min_confidence: Decimal
    opportunity_threshold: Decimal
    watch_metadata: dict[str, object]
    created_at: datetime


class TrendSignalRequest(BaseModel):
    topic: str = Field(min_length=1, max_length=500)
    provider_key: str = Field(min_length=1, max_length=64)
    external_id: str = Field(min_length=1, max_length=512)
    source_kind: str = Field(min_length=1, max_length=64)
    independence_key: str = Field(min_length=1, max_length=512)
    observed_at: datetime
    observation_key: str | None = Field(default=None, max_length=128)
    canonical_url: str | None = None
    source_name: str | None = Field(default=None, max_length=255)
    title: str | None = None
    body_excerpt: str | None = None
    author: str | None = Field(default=None, max_length=255)
    community: str | None = Field(default=None, max_length=255)
    language: str | None = Field(default=None, max_length=32)
    region: str | None = Field(default=None, max_length=64)
    published_at: datetime | None = None
    metrics: dict[str, float] = Field(default_factory=dict)
    media_refs: list[dict[str, object]] = Field(default_factory=list, max_length=100)
    aliases: list[str] = Field(default_factory=list, max_length=100)
    tags: list[str] = Field(default_factory=list, max_length=100)
    content_fingerprint: str | None = Field(default=None, max_length=128)
    metadata: dict[str, object] = Field(default_factory=dict)
    match_confidence: float = Field(default=1.0, ge=0, le=1)
    match_reasons: list[str] = Field(default_factory=list, max_length=50)


class TrendSignalResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

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


class TrendRefreshRequest(BaseModel):
    idempotency_key: str | None = Field(default=None, max_length=256)


class TrendRefreshResponse(BaseModel):
    channel_profile_id: uuid.UUID
    workflow_id: str
    run_key: str


class TrendOpportunityResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    channel_profile_id: uuid.UUID
    trend_topic_id: uuid.UUID
    watch_version: int
    run_key: str
    lifecycle: str
    opportunity_score: Decimal
    confidence: Decimal
    rank: int | None
    prediction_horizon_hours: int
    expires_at: datetime
    components: dict[str, float]
    reasons: list[str]
    evidence_summary: dict[str, object]
    created_at: datetime


class TrendEvidencePacketResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    trend_opportunity_id: uuid.UUID
    version: int
    packet_sha256: str
    thesis: str
    why_now: list[str]
    sources: list[dict[str, object]]
    claims: list[dict[str, object]]
    media_refs: list[dict[str, object]]
    acquisition_refs: list[dict[str, object]]
    packet_metadata: dict[str, object]
    generated_at: datetime


def _refresh_identity(channel_profile_id: uuid.UUID, key: str | None) -> tuple[str, str]:
    run_key = key.strip() if key and key.strip() else f"manual-{uuid.uuid4().hex}"
    digest = hashlib.sha256(run_key.encode()).hexdigest()[:20]
    return run_key, f"channel-trend-refresh-{channel_profile_id}-{digest}"


@router.post(
    "/channels/{channel_profile_id}/trends/watch-profile",
    response_model=TrendWatchResponse,
)
def update_trend_watch(
    channel_profile_id: uuid.UUID,
    request: TrendWatchRequest,
) -> ChannelTrendWatchVersion:
    try:
        return create_watch_profile(
            channel_profile_id,
            interests=request.interests,
            excluded_terms=request.excluded_terms,
            entities=request.entities,
            platforms=request.platforms,
            languages=request.languages,
            regions=request.regions,
            source_weights=request.source_weights,
            freshness_horizon_hours=request.freshness_horizon_hours,
            min_confidence=request.min_confidence,
            opportunity_threshold=request.opportunity_threshold,
            metadata=request.metadata,
            actor=request.actor,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get(
    "/channels/{channel_profile_id}/trends/watch-profile",
    response_model=TrendWatchResponse | None,
)
def get_trend_watch(channel_profile_id: uuid.UUID) -> ChannelTrendWatchVersion | None:
    return active_watch_profile(channel_profile_id)


@router.post(
    "/trends/signals",
    response_model=TrendSignalResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
def ingest_trend_signal(request: TrendSignalRequest) -> TrendSignal:
    try:
        return register_signal(
            topic=request.topic,
            provider_key=request.provider_key,
            external_id=request.external_id,
            source_kind=request.source_kind,
            independence_key=request.independence_key,
            observed_at=request.observed_at,
            observation_key=request.observation_key,
            canonical_url=request.canonical_url,
            source_name=request.source_name,
            title=request.title,
            body_excerpt=request.body_excerpt,
            author=request.author,
            community=request.community,
            language=request.language,
            region=request.region,
            published_at=request.published_at,
            metrics=request.metrics,
            media_refs=request.media_refs,
            aliases=request.aliases,
            tags=request.tags,
            content_fingerprint=request.content_fingerprint,
            metadata=request.metadata,
            match_confidence=request.match_confidence,
            match_reasons=request.match_reasons,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post(
    "/channels/{channel_profile_id}/trends/refresh",
    response_model=TrendRefreshResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def refresh_trends(
    channel_profile_id: uuid.UUID,
    request: TrendRefreshRequest,
) -> TrendRefreshResponse:
    if active_watch_profile(channel_profile_id) is None:
        raise HTTPException(status_code=409, detail="trend watch profile is not configured")
    run_key, workflow_id = _refresh_identity(channel_profile_id, request.idempotency_key)
    await start_trend_refresh_workflow(str(channel_profile_id), workflow_id, run_key)
    return TrendRefreshResponse(
        channel_profile_id=channel_profile_id,
        workflow_id=workflow_id,
        run_key=run_key,
    )


@router.get(
    "/channels/{channel_profile_id}/trends/opportunities",
    response_model=list[TrendOpportunityResponse],
)
def get_trend_opportunities(
    channel_profile_id: uuid.UUID,
    limit: int = Query(default=25, ge=1, le=100),
    min_score: float = Query(default=0.0, ge=0, le=1),
) -> list[TrendOpportunity]:
    return list_opportunities(channel_profile_id, limit=limit, min_score=min_score)


@router.get(
    "/trends/opportunities/{opportunity_id}/evidence",
    response_model=TrendEvidencePacketResponse | None,
)
def get_trend_evidence(opportunity_id: uuid.UUID) -> TrendEvidencePacket | None:
    return latest_evidence_packet(opportunity_id)
