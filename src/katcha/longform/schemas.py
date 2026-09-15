from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field, model_validator


class CandidateEvidence(BaseModel):
    clip_id: UUID
    deterministic_score: float = Field(ge=0, le=1)
    opening_score: float = Field(ge=0, le=1)
    duration_seconds: float = Field(gt=0)
    event_summary: str = ""
    categories: list[str] = Field(default_factory=list)
    tone: list[str] = Field(default_factory=list)
    source_creator: str | None = None
    short_production_id: UUID | None = None
    short_publication_id: UUID | None = None
    views: int = Field(default=0, ge=0)
    engaged_views: int = Field(default=0, ge=0)
    average_view_percentage: float = Field(default=0, ge=0)
    shares: int = Field(default=0, ge=0)
    comments: int = Field(default=0, ge=0)
    subscribers_gained: int = Field(default=0, ge=0)
    feature_score: float = Field(default=0, ge=0, le=100)
    hook_score: float = Field(default=0, ge=0, le=100)
    payoff_score: float = Field(default=0, ge=0, le=100)
    surprise_score: float = Field(default=0, ge=0, le=100)
    rewatch_score: float = Field(default=0, ge=0, le=100)
    evidence: dict[str, object] = Field(default_factory=dict)


class EditorSegmentPlan(BaseModel):
    clip_id: UUID
    host_before: str | None = Field(default=None, max_length=600)
    host_after: str | None = Field(default=None, max_length=600)
    transition_before: str | None = Field(default=None, max_length=300)
    source_start_seconds: float = Field(default=0, ge=0)
    source_end_seconds: float | None = Field(default=None, gt=0)
    reason: str = Field(default="", max_length=800)


class LongformEditorPlan(BaseModel):
    title_angle: str = Field(min_length=1, max_length=180)
    opening_hook: str = Field(min_length=1, max_length=800)
    intro: str | None = Field(default=None, max_length=1200)
    segments: list[EditorSegmentPlan] = Field(min_length=3, max_length=100)
    outro: str | None = Field(default=None, max_length=1200)
    pacing_notes: list[str] = Field(default_factory=list, max_length=20)

    @model_validator(mode="after")
    def unique_clips(self) -> LongformEditorPlan:
        clip_ids = [segment.clip_id for segment in self.segments]
        if len(clip_ids) != len(set(clip_ids)):
            raise ValueError("long-form editor plan cannot repeat a clip")
        return self


class CritiqueIssue(BaseModel):
    severity: Literal["low", "medium", "high"]
    issue: str = Field(min_length=1, max_length=800)
    recommendation: str = Field(min_length=1, max_length=800)
    clip_id: UUID | None = None


class LongformCritique(BaseModel):
    verdict: Literal["pass", "revise"]
    strengths: list[str] = Field(default_factory=list, max_length=12)
    issues: list[CritiqueIssue] = Field(default_factory=list, max_length=20)
    pacing_summary: str = Field(default="", max_length=1200)
    audience_fit_summary: str = Field(default="", max_length=1200)


class FinalLongformPlan(BaseModel):
    title_angle: str = Field(min_length=1, max_length=180)
    opening_hook: str = Field(min_length=1, max_length=800)
    intro: str | None = Field(default=None, max_length=1200)
    segments: list[EditorSegmentPlan] = Field(min_length=3, max_length=100)
    outro: str | None = Field(default=None, max_length=1200)
    pacing_notes: list[str] = Field(default_factory=list, max_length=20)
    revision_summary: str = Field(default="", max_length=1600)

    @model_validator(mode="after")
    def unique_clips(self) -> FinalLongformPlan:
        clip_ids = [segment.clip_id for segment in self.segments]
        if len(clip_ids) != len(set(clip_ids)):
            raise ValueError("final long-form plan cannot repeat a clip")
        return self
