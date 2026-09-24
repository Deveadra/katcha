from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, HttpUrl


class IngestRequest(BaseModel):
    url: HttpUrl
    force_retry: bool = False


class IngestResponse(BaseModel):
    source_id: UUID
    workflow_id: str
    status: str
    clip_id: UUID | None = None


class AnalyzeRequest(BaseModel):
    force_retry: bool = False


class AnalyzeResponse(BaseModel):
    analysis_run_id: UUID
    clip_id: UUID
    workflow_id: str
    status: str
    stage: str


class AnalysisRunResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    clip_id: UUID
    workflow_id: str
    status: str
    stage: str
    error: str | None
    escalation_reason: str | None
    started_at: datetime | None
    completed_at: datetime | None
    created_at: datetime
    updated_at: datetime


class ClipFeatureResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    clip_id: UUID
    contact_sheet_key: str | None
    keyframe_keys: list[str]
    perceptual_hashes: list[str]
    transcript: str | None
    transcript_language: str | None
    transcript_confidence: Decimal | None
    local_features: dict[str, object]
    ai_features: dict[str, object]
    candidate_score: Decimal | None
    score_breakdown: dict[str, object]
    updated_at: datetime


class CreateProductionRequest(BaseModel):
    persona_key: str = "youth_host"
    idempotency_key: str | None = Field(default=None, max_length=256)
    edit_blueprint_key: str | None = Field(default=None, min_length=1, max_length=96)


class ProductionResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    clip_id: UUID
    parent_production_id: UUID | None
    generation: int
    regenerate_from: str | None
    workflow_id: str
    kind: str
    status: str
    stage: str
    persona_key: str
    persona_version: str
    prompt_version: str
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


class ProductionScriptResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    production_id: UUID
    candidate_index: int
    style: str
    narration: str
    interaction_prompt: str | None
    rationale: str | None
    provider: str
    model: str
    prompt_version: str
    selected: bool
    script_metadata: dict[str, object]
    created_at: datetime


class ProductionAssetResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    production_id: UUID
    kind: str
    generation: int
    storage_key: str
    content_type: str | None
    provider: str | None
    model: str | None
    asset_metadata: dict[str, object]
    created_at: datetime


class ProductionReviewResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    production_id: UUID
    decision: str
    actor: str
    note: str | None
    review_metadata: dict[str, object]
    created_at: datetime


class ProductionDetailResponse(BaseModel):
    production: ProductionResponse
    scripts: list[ProductionScriptResponse]
    assets: list[ProductionAssetResponse]
    reviews: list[ProductionReviewResponse]


class ReviewProductionRequest(BaseModel):
    decision: Literal["approve", "reject", "regenerate"]
    note: str | None = Field(default=None, max_length=2000)
    actor: str = Field(default="operator", min_length=1, max_length=128)
    regenerate_from: Literal["script", "voice", "render"] = "script"


class ReviewActionResponse(BaseModel):
    production_id: UUID
    decision: str
    child_production_id: UUID | None = None
    child_workflow_id: str | None = None
    publication_id: UUID | None = None
    publication_workflow_id: str | None = None
    auto_publication_action: str | None = None
    auto_publication_reason: str | None = None


class CreateCompilationRequest(BaseModel):
    theme: str = Field(min_length=1, max_length=500)
    target_duration_seconds: int | None = Field(default=None, ge=180, le=7200)
    target_segment_count: int | None = Field(default=None, ge=3, le=100)
    persona_key: str = Field(default="youth_host", min_length=1, max_length=64)
    idempotency_key: str | None = Field(default=None, max_length=256)


class CompilationResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    parent_compilation_id: UUID | None
    generation: int
    regenerate_from: str | None
    workflow_id: str
    status: str
    stage: str
    theme: str
    target_duration_seconds: int
    target_segment_count: int | None
    persona_key: str
    persona_version: str
    prompt_version: str
    candidate_snapshot: dict[str, object]
    editor_plan: dict[str, object]
    critic_feedback: dict[str, object]
    final_plan: dict[str, object]
    selected_voice_profile: str | None
    render_manifest: dict[str, object]
    estimated_cost_usd: Decimal
    error: str | None
    created_at: datetime
    updated_at: datetime


class CompilationSegmentResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    compilation_id: UUID
    position: int
    clip_id: UUID
    short_production_id: UUID | None
    short_publication_id: UUID | None
    deterministic_score: Decimal
    opening_score: Decimal
    source_duration_seconds: Decimal
    source_start_seconds: Decimal
    source_end_seconds: Decimal | None
    transition_before: str | None
    host_before: str | None
    host_after: str | None
    selection_reason: str | None
    evidence: dict[str, object]
    timing: dict[str, object]
    created_at: datetime


class CompilationAssetResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    compilation_id: UUID
    kind: str
    generation: int
    storage_key: str
    content_type: str | None
    provider: str | None
    model: str | None
    asset_metadata: dict[str, object]
    created_at: datetime


class CompilationReviewResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    compilation_id: UUID
    decision: str
    actor: str
    note: str | None
    review_metadata: dict[str, object]
    created_at: datetime


class CompilationDetailResponse(BaseModel):
    compilation: CompilationResponse
    segments: list[CompilationSegmentResponse]
    assets: list[CompilationAssetResponse]
    reviews: list[CompilationReviewResponse]


class ReviewCompilationRequest(BaseModel):
    decision: Literal["approve", "reject", "regenerate"]
    note: str | None = Field(default=None, max_length=2000)
    actor: str = Field(default="operator", min_length=1, max_length=128)
    regenerate_from: Literal["plan", "voice", "render"] = "plan"


class ReviewCompilationResponse(BaseModel):
    compilation_id: UUID
    decision: str
    child_compilation_id: UUID | None = None
    child_workflow_id: str | None = None


class YouTubeOAuthStartResponse(BaseModel):
    authorization_url: str


class YouTubeConnectionResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    channel_id: str
    channel_title: str
    status: str
    scopes: list[str]
    token_expires_at: datetime
    last_refreshed_at: datetime | None
    created_at: datetime
    updated_at: datetime


class CreatePublicationRequest(BaseModel):
    youtube_connection_id: UUID
    title: str = Field(min_length=1, max_length=100)
    description: str = Field(default="", max_length=5000)
    tags: list[str] = Field(default_factory=list, max_length=50)
    category_id: str | None = Field(default=None, max_length=32)
    privacy_status: Literal["private", "unlisted", "public"] = "private"
    publish_at: datetime | None = None
    notify_subscribers: bool = False
    made_for_kids: bool = False
    contains_synthetic_media: bool = False


class PublicationResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    production_id: UUID | None
    compilation_id: UUID | None
    short_episode_id: UUID | None
    youtube_connection_id: UUID
    workflow_id: str
    workflow_attempt: int
    analytics_workflow_id: str
    status: str
    stage: str
    title: str
    description: str
    tags: list[str]
    category_id: str
    privacy_status: str
    publish_at: datetime | None
    notify_subscribers: bool
    made_for_kids: bool
    contains_synthetic_media: bool
    treatment_metadata: dict[str, object]
    youtube_video_id: str | None
    upload_offset: int
    upload_size: int | None
    processing_status: str | None
    failure_reason: str | None
    rejection_reason: str | None
    raw_status: dict[str, object]
    published_at: datetime | None
    error: str | None
    created_at: datetime
    updated_at: datetime


class RetryPublicationRequest(BaseModel):
    allow_new_upload_session: bool = False


class PublicationAnalyticsSnapshotResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    publication_id: UUID
    sample_key: str
    sampled_at: datetime
    period_start: date
    period_end: date
    views: int | None
    engaged_views: int | None
    estimated_minutes_watched: Decimal | None
    average_view_duration: Decimal | None
    average_view_percentage: Decimal | None
    likes: int | None
    comments: int | None
    shares: int | None
    subscribers_gained: int | None
    subscribers_lost: int | None
    estimated_revenue: Decimal | None
    estimated_ad_revenue: Decimal | None
    monetized_playbacks: int | None
    raw_metrics: dict[str, object]
    raw_monetary: dict[str, object]
    raw_video: dict[str, object]
    created_at: datetime


class RetentionPointResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    snapshot_id: UUID
    elapsed_video_time_ratio: Decimal
    audience_watch_ratio: Decimal | None
    relative_retention_performance: Decimal | None
    raw_row: dict[str, object]


class AnalyticsSnapshotDetailResponse(BaseModel):
    snapshot: PublicationAnalyticsSnapshotResponse
    retention: list[RetentionPointResponse]


class AnalyticsRefreshResponse(BaseModel):
    publication_id: UUID
    workflow_id: str
    sample_key: str


class SourceResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    source_url: str
    canonical_url: str
    platform: str
    status: str
    title: str | None
    creator: str | None
    workflow_id: str | None
    error: str | None
    clip_id: UUID | None
    discovered_at: datetime
    updated_at: datetime


class ClipResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    sha256: str
    storage_key: str
    extension: str | None
    size_bytes: int | None
    duration_seconds: Decimal | None
    width: int | None
    height: int | None
    status: str
    created_at: datetime
    updated_at: datetime


class HealthResponse(BaseModel):
    status: str = Field(examples=["ok"])
    service: str = "katcha"
    version: str
