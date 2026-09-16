from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import and_, func, select

from katcha.acquisition.adapters import get_adapter
from katcha.acquisition_models import (
    CandidateTrendScore,
    DiscoveryCandidate,
    TopicWatchVersion,
)
from katcha.db import session_scope
from katcha.orchestration.client import start_discovery_workflow
from katcha.services.acquisition import register_discovery_run
from katcha.services.trends import (
    compute_candidate_trend_score,
    create_topic_watch_version,
)

router = APIRouter(prefix="/v1/trends", tags=["trends"])
_SECRET_FRAGMENTS = ("authorization", "credential", "password", "secret", "token", "api_key")


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


class TopicWatchRunResponse(BaseModel):
    discovery_run_id: uuid.UUID
    adapter_key: str
    adapter_version: str
    workflow_id: str


class ExecuteTopicWatchResponse(BaseModel):
    topic_watch_id: uuid.UUID
    watch_key: str
    version: int
    execution_key: str
    runs: list[TopicWatchRunResponse]


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


class ManualTrendScoreRequest(BaseModel):
    score_key: str | None = Field(default=None, max_length=160)
    related_item_count: int = Field(default=0, ge=0)
    corroborating_source_count: int | None = Field(default=None, ge=0)


def _contains_secret_key(value: object) -> bool:
    if isinstance(value, dict):
        for key, child in value.items():
            normalized = str(key).casefold().replace("-", "_")
            if any(fragment in normalized for fragment in _SECRET_FRAGMENTS):
                return True
            if _contains_secret_key(child):
                return True
    elif isinstance(value, list):
        return any(_contains_secret_key(item) for item in value)
    return False


def _adapter_config(raw: dict[str, Any]) -> tuple[str, str, dict[str, Any]]:
    if _contains_secret_key(raw):
        raise ValueError(
            "topic watch adapter configuration must not contain credentials or tokens"
        )
    adapter_key = str(raw.get("adapter_key") or "").strip()
    adapter_version = str(raw.get("adapter_version") or "v1").strip()
    query = raw.get("query", {})
    if not adapter_key:
        raise ValueError("topic watch adapter config is missing adapter_key")
    if not isinstance(query, dict):
        raise ValueError("topic watch adapter config query must be an object")
    get_adapter(adapter_key, adapter_version)
    return adapter_key, adapter_version, dict(query)


@router.post(
    "/watches",
    response_model=TopicWatchResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_topic_watch(request: CreateTopicWatchRequest) -> TopicWatchVersion:
    try:
        for config in request.adapter_configs:
            _adapter_config(config)
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
    with session_scope() as session:
        watch = session.get(TopicWatchVersion, topic_watch_id)
        if watch is None:
            raise HTTPException(status_code=404, detail="topic watch version not found")
        if not watch.enabled:
            raise HTTPException(status_code=409, detail="topic watch is disabled")
        watch_key = watch.watch_key
        watch_version = watch.version
        include_terms = list(watch.include_terms or [])
        exclude_terms = list(watch.exclude_terms or [])
        max_candidates = watch.max_candidates
        configs = [dict(item) for item in (watch.adapter_configs or [])]
    if not configs:
        raise HTTPException(status_code=409, detail="topic watch has no adapters configured")

    execution_key = request.idempotency_key or uuid.uuid4().hex
    runs: list[TopicWatchRunResponse] = []
    for index, config in enumerate(configs):
        try:
            adapter_key, adapter_version, query = _adapter_config(config)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        query["include_terms"] = include_terms
        query["exclude_terms"] = exclude_terms
        requested_limit = int(query.get("limit", max_candidates))
        query["limit"] = max(1, min(requested_limit, max_candidates))
        run_key = f"watch:{topic_watch_id}:{execution_key}:{index}"
        run = register_discovery_run(
            adapter_key=adapter_key,
            adapter_version=adapter_version,
            query=query,
            idempotency_key=run_key,
            metadata={
                "topic_watch_id": str(topic_watch_id),
                "watch_key": watch_key,
                "watch_version": watch_version,
                "execution_key": execution_key,
                "adapter_index": index,
            },
        )
        workflow_id = f"discovery-run-{run.id}"
        await start_discovery_workflow(str(run.id), workflow_id)
        runs.append(
            TopicWatchRunResponse(
                discovery_run_id=run.id,
                adapter_key=adapter_key,
                adapter_version=adapter_version,
                workflow_id=workflow_id,
            )
        )
    return ExecuteTopicWatchResponse(
        topic_watch_id=topic_watch_id,
        watch_key=watch_key,
        version=watch_version,
        execution_key=execution_key,
        runs=runs,
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
