from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import and_, func, select

from katcha.acquisition_models import TopicWatchVersion
from katcha.db import session_scope
from katcha.services.discovery_trends import create_topic_watch_version
from katcha.services.trend_execution import normalize_adapter_config
from katcha.services.trend_source_reliability import (
    channel_trend_source_health,
    topic_watch_source_health,
    watch_scope_key,
)

router = APIRouter(tags=["trend-source-reliability"])


class CreateChannelTopicWatchRequest(BaseModel):
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


class ChannelTopicWatchResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    channel_profile_id: uuid.UUID | None
    scope_key: str
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


@router.post(
    "/channels/{channel_profile_id}/trends/watches",
    response_model=ChannelTopicWatchResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_channel_topic_watch(
    channel_profile_id: uuid.UUID,
    request: CreateChannelTopicWatchRequest,
) -> TopicWatchVersion:
    try:
        for config in request.adapter_configs:
            normalize_adapter_config(config)
        return create_topic_watch_version(
            channel_profile_id=channel_profile_id,
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
        code = 404 if "channel profile not found" in str(exc) else 409
        raise HTTPException(status_code=code, detail=str(exc)) from exc


@router.get(
    "/channels/{channel_profile_id}/trends/watches",
    response_model=list[ChannelTopicWatchResponse],
)
def list_channel_topic_watches(
    channel_profile_id: uuid.UUID,
    limit: int = Query(default=100, ge=1, le=500),
    enabled: bool | None = Query(default=None),
) -> list[TopicWatchVersion]:
    scope = watch_scope_key(channel_profile_id)
    with session_scope() as session:
        latest = (
            select(
                TopicWatchVersion.watch_key.label("watch_key"),
                func.max(TopicWatchVersion.version).label("version"),
            )
            .where(TopicWatchVersion.scope_key == scope)
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
            .where(TopicWatchVersion.scope_key == scope)
            .order_by(TopicWatchVersion.watch_key)
            .limit(limit)
        )
        if enabled is not None:
            stmt = stmt.where(TopicWatchVersion.enabled == enabled)
        return list(session.scalars(stmt))


@router.get("/trends/watches/{topic_watch_id}/health")
def get_topic_watch_source_health(topic_watch_id: uuid.UUID) -> dict[str, Any]:
    try:
        return topic_watch_source_health(topic_watch_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/channels/{channel_profile_id}/trends/source-health")
def get_channel_source_health(channel_profile_id: uuid.UUID) -> dict[str, Any]:
    return channel_trend_source_health(channel_profile_id)
