from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class ShortEpisodeCandidateRequest(BaseModel):
    clip_id: UUID
    hook_strength: float = Field(ge=0, le=100)
    visual_clarity: float = Field(ge=0, le=100)
    payoff_strength: float = Field(ge=0, le=100)
    escalation_value: float = Field(ge=0, le=100)
    commentary_opportunity: float = Field(ge=0, le=100)
    novelty: float = Field(ge=0, le=100)
    source_quality: float = Field(ge=0, le=100)


class CreateShortEpisodeRequest(BaseModel):
    channel_profile_id: UUID
    premise: str = Field(min_length=1, max_length=500)
    candidates: list[ShortEpisodeCandidateRequest] = Field(min_length=3, max_length=50)
    item_count: Literal[3, 5, 7] | None = None
    format_key: str | None = Field(default=None, min_length=1, max_length=64)
    format_version: str | None = Field(default=None, min_length=1, max_length=32)
    idempotency_key: str | None = Field(default=None, max_length=256)


class ShortEpisodeResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    channel_profile_id: UUID
    parent_episode_id: UUID | None
    generation: int
    regenerate_from: str | None
    workflow_id: str
    status: str
    stage: str
    premise: str
    format_key: str
    format_version: str
    item_count: int
    format_snapshot: dict[str, object]
    plan_snapshot: dict[str, object]
    persona_key: str
    persona_version: str
    brand_key: str
    brand_version: int
    brand_snapshot: dict[str, object]
    estimated_cost_usd: Decimal
    error: str | None
    created_at: datetime
    updated_at: datetime


class ShortEpisodeItemResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    short_episode_id: UUID
    position: int
    clip_id: UUID
    role: str
    overall_score: Decimal
    role_score: Decimal
    editorial_signals: dict[str, object]
    analysis_snapshot: dict[str, object]
    acquisition_snapshot: dict[str, object]
    source_snapshot: list[dict[str, object]]
    created_at: datetime


class ShortEpisodeDetailResponse(BaseModel):
    episode: ShortEpisodeResponse
    items: list[ShortEpisodeItemResponse]
