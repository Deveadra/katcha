from __future__ import annotations

from datetime import datetime
from decimal import Decimal
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
