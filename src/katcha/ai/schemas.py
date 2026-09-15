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
