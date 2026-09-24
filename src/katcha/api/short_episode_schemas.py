from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class ShortEpisodeCandidateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    clip_id: UUID
    hook_strength: float = Field(ge=0, le=100)
    visual_clarity: float = Field(ge=0, le=100)
    payoff_strength: float = Field(ge=0, le=100)
    escalation_value: float = Field(ge=0, le=100)
    commentary_opportunity: float = Field(ge=0, le=100)
    novelty: float = Field(ge=0, le=100)
    source_quality: float = Field(ge=0, le=100)


class CreateShortEpisodeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    channel_profile_id: UUID
    premise: str = Field(min_length=1, max_length=500)
    candidates: list[ShortEpisodeCandidateRequest] = Field(min_length=3, max_length=50)
    item_count: Literal[3, 5, 7] | None = None
    format_key: str | None = Field(default=None, min_length=1, max_length=64)
    format_version: str | None = Field(default=None, min_length=1, max_length=32)
    edit_blueprint_key: str | None = Field(default=None, min_length=1, max_length=96)
    idempotency_key: str | None = Field(default=None, max_length=256)
    trend_opportunity_id: UUID | None = None


class StartShortEpisodeEditorialRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    start_stage: Literal["script", "voice", "render"] = "script"


class StartShortEpisodeEditorialResponse(BaseModel):
    episode_id: UUID
    workflow_id: str
    start_stage: str
    status: str


class ReviewShortEpisodeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: Literal["approve", "reject", "regenerate"]
    note: str | None = Field(default=None, max_length=2000)
    actor: str = Field(default="operator", min_length=1, max_length=128)
    regenerate_from: Literal["script", "voice", "render"] = "script"


class ReviewShortEpisodeResponse(BaseModel):
    episode_id: UUID
    decision: str
    child_episode_id: UUID | None = None
    child_workflow_id: str | None = None


class ShortEpisodeResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    channel_profile_id: UUID
    trend_opportunity_id: UUID | None
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
    prompt_version: str
    brand_key: str
    brand_version: int
    brand_snapshot: dict[str, object]
    edit_blueprint_key: str | None
    edit_blueprint_version: int | None
    edit_blueprint_snapshot: dict[str, object] | None
    selected_script_id: UUID | None
    selected_voice_profile: str | None
    render_manifest: dict[str, object]
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


class ShortEpisodeScriptResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    short_episode_id: UUID
    candidate_index: int
    style: str
    script_payload: dict[str, object]
    narration_beats: list[dict[str, object]]
    provider: str
    model: str
    prompt_version: str
    selected: bool
    created_at: datetime


class ShortEpisodeAssetResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    short_episode_id: UUID
    kind: str
    generation: int
    storage_key: str
    content_type: str
    provider: str | None
    model: str | None
    asset_metadata: dict[str, object]
    created_at: datetime


class ShortEpisodeReviewResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    short_episode_id: UUID
    decision: str
    actor: str
    note: str | None
    review_metadata: dict[str, object]
    created_at: datetime


class ShortEpisodeDetailResponse(BaseModel):
    episode: ShortEpisodeResponse
    items: list[ShortEpisodeItemResponse]
    scripts: list[ShortEpisodeScriptResponse] = Field(default_factory=list)
    assets: list[ShortEpisodeAssetResponse] = Field(default_factory=list)
    reviews: list[ShortEpisodeReviewResponse] = Field(default_factory=list)
