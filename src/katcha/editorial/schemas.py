from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator


class CommentarySegment(BaseModel):
    placement: Literal["pre", "mid", "post"]
    text: str = Field(min_length=1, max_length=320)
    purpose: str = Field(default="commentary", max_length=120)
    source_time_seconds: float | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def validate_midpoint(self) -> CommentarySegment:
        if self.placement == "mid" and self.source_time_seconds is None:
            raise ValueError("mid commentary requires source_time_seconds")
        if self.placement != "mid" and self.source_time_seconds is not None:
            raise ValueError("only mid commentary may specify source_time_seconds")
        return self


class ShortScriptCandidate(BaseModel):
    style: Literal["observational", "sarcastic", "interactive"]
    segments: list[CommentarySegment] = Field(min_length=1, max_length=3)
    interaction_prompt: str | None = Field(default=None, max_length=240)
    title_angle: str = Field(default="", max_length=160)
    rationale: str = Field(default="", max_length=500)

    @model_validator(mode="after")
    def enforce_interaction_placement(self) -> ShortScriptCandidate:
        if self.interaction_prompt and not any(
            segment.placement == "post" for segment in self.segments
        ):
            raise ValueError("interaction prompts require post-clip commentary")
        return self

    @property
    def narration(self) -> str:
        return " ".join(segment.text.strip() for segment in self.segments).strip()


class ShortScriptSet(BaseModel):
    candidates: list[ShortScriptCandidate] = Field(min_length=3, max_length=3)

    @model_validator(mode="after")
    def require_distinct_styles(self) -> ShortScriptSet:
        styles = {candidate.style for candidate in self.candidates}
        required = {"observational", "sarcastic", "interactive"}
        if styles != required:
            raise ValueError("script set must contain exactly one candidate for each style")
        return self
