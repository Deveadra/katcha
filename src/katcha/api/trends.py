from __future__ import annotations

import hashlib
import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import and_, func, select

from katcha.acquisition_models import (
    CandidateTrendScore,
    DiscoveryCandidate,
    TopicWatchVersion,
)
from katcha.db import session_scope
from katcha.orchestration.trend_client import (
    start_topic_watch_schedule,
    start_topic_watch_workflow,
)
from katcha.services.trend_execution import normalize_adapter_config
from katcha.services.trends import (
    compute_candidate_trend_score,
    create_topic_watch_version,
)
from katcha.trend_models import TrendReviewQueueItem

router = APIRouter(prefix="/v1/trends", tags=["trends"])


class CreateTopicWatchRequest(BaseModel):
    watch_key: str = Field(min_length=1, max_length=128)
    name: str = Field(min_length=1, max_length=255)
    include_terms: list[str] = Field(default_factory=list, max_length=100)
    exclude_terms: list[str] = Field(default_factory=list, max_length=100)
    adapter_configs: list[dict[str, Any]] = Field(default_factory=list, max_length=50)
    language: str | None = Field(default=None, max_length=32)
    locale: str | None = Field(default=None, max_length=64)
    freshness_horizon_hours: int = Field(default=72, ge=1, le=24 * 30)
    max_candidates: int = Field(default=100, ge=1, le=1000)
    enabled: bool = True
    metadata: dict[str, Any] = Field(default_factory=dict)


class TopicWatchResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    watch_key: str
    version: int
    name: str
    enabled: bool
    include_terms: list[str]
    exclude_terms: list[str]
    adapter_configs: list[dict[str, Any]]
    language: str | None
    locale: str | None
    freshness_horizon_hours: int
    max_candidates: int
    watch_metadata: dict[str, Any]
    created_at: datetime


class ExecuteTopicWatchRequest(BaseModel):
    idempotency_key: str | None = Field(default=None, max_length=80)
    top_n: int = Field(default=25, ge=1, le=250)


class ExecuteTopicWatchResponse(BaseModel):
    topic_watch_id: uuid.UUID
    watch_key: str
    version: int
    execution_key: str
    workflow_id: str
    top_n: int


class ScheduleTopicWatchRequest(BaseModel):
    interval_minutes: int = Field(default=60, ge=5, le=7 * 24 * 60)
    top_n: int = Field(default=25, ge=1, le=250)


class ScheduleTopicWatchResponse(BaseModel):
    topic_watch_id: uuid.UUID
    watch_key: str
    version: int
    workflow_id: str
    interval_minutes: int
    top_n: int


class TrendScoreResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    topic_watch_id: uuid.UUID
    discovery_candidate_id: uuid.UUID
    version: int
    score_key: str
    algorithm_version: str
    score: Decimal
    feature_breakdown: dict[str, Any]
    observation_count: int
    window_start: datetime | None
    window_end: datetime | None
    computed_at: datetime


class RankedTrendCandidateResponse(BaseModel):
    candidate_id: uuid.UUID
    title: str | None
    creator: str | None
    source_url: str
    platform: str
    trend: TrendScoreResponse


class TrendQueueItemResponse(BaseModel):
    id: uuid.UUID
    queue_key: str
    rank: int
    status: str
    candidate_id: uuid.UUID
    title: str | None
    creator: str | None
    source_url: str
    platform: str
    queue_metadata: dict[str, Any]
    trend: TrendScoreResponse


class ManualTrendScoreRequest(BaseModel):
    score_key: str | None = Field(default=None, max_length=160)
    related_item_count: int = Field(default=0, ge=0)
    corroborating_source_count: int | None = Field(default=None, ge=0)


def _watch_snapshot(topic_watch_id: uuid.UUID) -> tuple[str, int, bool]:
    with session_scope() as session:
        watch = session.get(TopicWatchVersion, topic_watch_id)
        if watch is None:
            raise HTTPException(status_code=404, detail="topic watch version not found")
        return watch.watch_key, watch.version, watch.enabled


@router.post(
    "/watches",
    response_model=TopicWatchResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_topic_watch(request: CreateTopicWatchRequest) -> TopicWatchVersion:
    try:
        for config in request.adapter_configs:
            normalize_adapter_config(config)
        return create_topic_watch_version(
            watch_key=request.watch_key,
            name=request.name,
            include_terms=request.include_terms,
            exclude_terms=request.exclude_terms,
            adapter_configs=request.adapter_configs,
            language=request.language,
            locale=request.locale,
            freshness_horizon_hours=request.freshness_horizon_hours,
            max_candidates=request.max_candidates,
            enabled=request.enabled,
            metadata=request.metadata,
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/watches", response_model=list[TopicWatchResponse])
def list_topic_watches(
    limit: int = Query(default=100, ge=1, le=500),
    enabled: bool | None = Query(default=None),
) -> list[TopicWatchVersion]:
    with session_scope() as session:
        latest = (
            select(
                TopicWatchVersion.watch_key.label("watch_key"),
                func.max(TopicWatchVersion.version).label("version"),
            )
            .group_by(TopicWatchVersion.watch_key)
            .subquery()
        )
        stmt = (
            select(TopicWatchVersion)
            .join(
                latest,
                and_(
                    TopicWatchVersion.watch_key == latest.c.watch_key,
                    TopicWatchVersion.version == latest.c.version,
                ),
            )
            .order_by(TopicWatchVersion.watch_key)
            .limit(limit)
        )
        if enabled is not None:
            stmt = stmt.where(TopicWatchVersion.enabled == enabled)
        return list(session.scalars(stmt))


@router.post(
    "/watches/{topic_watch_id}/execute",
    response_model=ExecuteTopicWatchResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def execute_topic_watch(
    topic_watch_id: uuid.UUID,
    request: ExecuteTopicWatchRequest,
) -> ExecuteTopicWatchResponse:
    watch_key, watch_version, enabled = _watch_snapshot(topic_watch_id)
    if not enabled:
        raise HTTPException(status_code=409, detail="topic watch is disabled")
    execution_key = request.idempotency_key or uuid.uuid4().hex
    digest = hashlib.sha256(execution_key.encode()).hexdigest()[:20]
    workflow_id = f"topic-watch-{topic_watch_id}-{digest}"
    await start_topic_watch_workflow(
        str(topic_watch_id),
        workflow_id,
        execution_key=execution_key,
        top_n=request.top_n,
    )
    return ExecuteTopicWatchResponse(
        topic_watch_id=topic_watch_id,
        watch_key=watch_key,
        version=watch_version,
        execution_key=execution_key,
        workflow_id=workflow_id,
        top_n=request.top_n,
    )


@router.post(
    "/watches/{topic_watch_id}/schedule",
    response_model=ScheduleTopicWatchResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def schedule_topic_watch(
    topic_watch_id: uuid.UUID,
    request: ScheduleTopicWatchRequest,
) -> ScheduleTopicWatchResponse:
    watch_key, watch_version, enabled = _watch_snapshot(topic_watch_id)
    if not enabled:
        raise HTTPException(status_code=409, detail="topic watch is disabled")
    workflow_id = f"topic-watch-schedule-{topic_watch_id}"
    await start_topic_watch_schedule(
        str(topic_watch_id),
        workflow_id,
        interval_minutes=request.interval_minutes,
        top_n=request.top_n,
    )
    return ScheduleTopicWatchResponse(
        topic_watch_id=topic_watch_id,
        watch_key=watch_key,
        version=watch_version,
        workflow_id=workflow_id,
        interval_minutes=request.interval_minutes,
        top_n=request.top_n,
    )


@router.post(
    "/watches/{topic_watch_id}/candidates/{candidate_id}/score",
    response_model=TrendScoreResponse,
)
def score_candidate_for_watch(
    topic_watch_id: uuid.UUID,
    candidate_id: uuid.UUID,
    request: ManualTrendScoreRequest,
) -> CandidateTrendScore:
    try:
        return compute_candidate_trend_score(
            candidate_id,
            topic_watch_id,
            score_key=request.score_key,
            related_item_count=request.related_item_count,
            corroborating_source_count=request.corroborating_source_count,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get(
    "/watches/{topic_watch_id}/ranked",
    response_model=list[RankedTrendCandidateResponse],
)
def ranked_topic_watch_candidates(
    topic_watch_id: uuid.UUID,
    limit: int = Query(default=50, ge=1, le=500),
) -> list[RankedTrendCandidateResponse]:
    with session_scope() as session:
        if session.get(TopicWatchVersion, topic_watch_id) is None:
            raise HTTPException(status_code=404, detail="topic watch version not found")
        latest = (
            select(
                CandidateTrendScore.discovery_candidate_id.label("candidate_id"),
                func.max(CandidateTrendScore.version).label("version"),
            )
            .where(CandidateTrendScore.topic_watch_id == topic_watch_id)
            .group_by(CandidateTrendScore.discovery_candidate_id)
            .subquery()
        )
        rows = list(
            session.execute(
                select(DiscoveryCandidate, CandidateTrendScore)
                .join(
                    latest,
                    latest.c.candidate_id == DiscoveryCandidate.id,
                )
                .join(
                    CandidateTrendScore,
                    and_(
                        CandidateTrendScore.topic_watch_id == topic_watch_id,
                        CandidateTrendScore.discovery_candidate_id
                        == DiscoveryCandidate.id,
                        CandidateTrendScore.version == latest.c.version,
                    ),
                )
                .order_by(CandidateTrendScore.score.desc())
                .limit(limit)
            )
        )
        return [
            RankedTrendCandidateResponse(
                candidate_id=candidate.id,
                title=candidate.title,
                creator=candidate.creator,
                source_url=candidate.source_url,
                platform=candidate.platform,
                trend=TrendScoreResponse.model_validate(score),
            )
            for candidate, score in rows
        ]


@router.get(
    "/watches/{topic_watch_id}/queue",
    response_model=list[TrendQueueItemResponse],
)
def topic_watch_queue(
    topic_watch_id: uuid.UUID,
    queue_key: str | None = Query(default=None, max_length=160),
    item_status: str | None = Query(default=None, max_length=32),
    limit: int = Query(default=100, ge=1, le=500),
) -> list[TrendQueueItemResponse]:
    with session_scope() as session:
        if session.get(TopicWatchVersion, topic_watch_id) is None:
            raise HTTPException(status_code=404, detail="topic watch version not found")
        selected_queue_key = queue_key
        if selected_queue_key is None:
            selected_queue_key = session.scalar(
                select(TrendReviewQueueItem.queue_key)
                .where(TrendReviewQueueItem.topic_watch_id == topic_watch_id)
                .order_by(TrendReviewQueueItem.created_at.desc())
                .limit(1)
            )
        if not selected_queue_key:
            return []
        stmt = (
            select(TrendReviewQueueItem, DiscoveryCandidate, CandidateTrendScore)
            .join(
                DiscoveryCandidate,
                TrendReviewQueueItem.discovery_candidate_id == DiscoveryCandidate.id,
            )
            .join(
                CandidateTrendScore,
                TrendReviewQueueItem.trend_score_id == CandidateTrendScore.id,
            )
            .where(
                TrendReviewQueueItem.topic_watch_id == topic_watch_id,
                TrendReviewQueueItem.queue_key == selected_queue_key,
            )
            .order_by(TrendReviewQueueItem.rank)
            .limit(limit)
        )
        if item_status is not None:
            stmt = stmt.where(TrendReviewQueueItem.status == item_status)
        rows = list(session.execute(stmt))
        return [
            TrendQueueItemResponse(
                id=item.id,
                queue_key=item.queue_key,
                rank=item.rank,
                status=item.status,
                candidate_id=candidate.id,
                title=candidate.title,
                creator=candidate.creator,
                source_url=candidate.source_url,
                platform=candidate.platform,
                queue_metadata=dict(item.queue_metadata or {}),
                trend=TrendScoreResponse.model_validate(score),
            )
            for item, candidate, score in rows
        ]
