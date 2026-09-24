from __future__ import annotations

from pydantic import BaseModel, Field


class ClipVisionResult(BaseModel):
    event_summary: str = Field(min_length=1, max_length=600)
    categories: list[str] = Field(default_factory=list, max_length=12)
    tone: list[str] = Field(default_factory=list, max_length=12)
    setup: str = Field(default="", max_length=500)
    payoff: str = Field(default="", max_length=500)
    hook_score: float = Field(ge=0, le=100)
    surprise_score: float = Field(ge=0, le=100)
    humor_score: float = Field(ge=0, le=100)
    comment_potential: float = Field(ge=0, le=100)
    rewatch_potential: float = Field(ge=0, le=100)
    context_required: bool = False
    requires_deep_video: bool = False
    deep_video_reason: str | None = Field(default=None, max_length=500)
    confidence: float = Field(ge=0, le=1)


class DeepVideoResult(ClipVisionResult):
    timeline: list[str] = Field(default_factory=list, max_length=20)
    audio_relevance: str = Field(default="", max_length=500)
    best_commentary_moments: list[str] = Field(default_factory=list, max_length=12)


class PackagingThumbnailBrief(BaseModel):
    concept: str = Field(min_length=1, max_length=500)
    focal_subject: str = Field(min_length=1, max_length=220)
    composition: str = Field(min_length=1, max_length=320)
    on_image_text: str | None = Field(default=None, max_length=80)
    emotion: str = Field(default="", max_length=120)
    avoid: list[str] = Field(default_factory=list, max_length=12)


class PackagingCandidate(BaseModel):
    variation_family: str = Field(min_length=1, max_length=64)
    angle: str = Field(min_length=1, max_length=220)
    title: str = Field(min_length=1, max_length=100)
    description: str = Field(default="", max_length=5000)
    supporting_facts: list[str] = Field(min_length=1, max_length=8)
    thumbnail: PackagingThumbnailBrief


class PackagingCandidateSet(BaseModel):
    candidates: list[PackagingCandidate] = Field(min_length=2, max_length=5)
