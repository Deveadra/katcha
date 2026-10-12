"""Measured visual evidence; indexing never grants rights or invents descriptions."""

from __future__ import annotations

import uuid
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from katcha.editorial.project_schemas import Sha256


class MeasuredShotV1(BaseModel):
    model_config = ConfigDict(extra="forbid")
    shot_id: str
    start_tick: int = Field(ge=0)
    end_tick: int = Field(gt=0)
    keyframe_tick: int = Field(ge=0)
    keyframe_sha256: Sha256
    visual_tags: list[str] = Field(max_length=8)
    mean_rgb: tuple[int, int, int]

    @model_validator(mode="after")
    def measured_range(self) -> Self:
        if not self.start_tick <= self.keyframe_tick < self.end_tick:
            raise ValueError("Keyframe must lie within its measured shot interval")
        if any(not 0 <= value <= 255 for value in self.mean_rgb):
            raise ValueError("Color evidence must be measured RGB values")
        return self


class SceneIndexV1(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal["SceneIndexV1"] = "SceneIndexV1"
    clip_sha256: Sha256
    algorithm_version: Literal["rgb-frame-difference-v1"] = "rgb-frame-difference-v1"
    policy_version: Literal["bounded-local-video-v1"] = "bounded-local-video-v1"
    decoder_version: str
    timebase_numerator: int = Field(gt=0)
    timebase_denominator: int = Field(gt=0)
    source_origin_tick: int
    duration_ticks: int = Field(gt=0)
    decoded_frames: int = Field(gt=0, le=36000)
    coverage: Literal["all_decoded_frames_visual_change_sparse_keyframes"] = (
        "all_decoded_frames_visual_change_sparse_keyframes"
    )
    shots: list[MeasuredShotV1] = Field(min_length=1, max_length=512)
    ocr_evidence: list[dict[str, Any]] = Field(default_factory=list, max_length=0)
    speech_evidence: list[dict[str, Any]] = Field(default_factory=list, max_length=0)
    vision_provider_calls: Literal[0] = 0

    @model_validator(mode="after")
    def continuous_measurements(self) -> Self:
        if self.decoded_frames < len(self.shots) or len(
            {shot.shot_id for shot in self.shots}
        ) != len(self.shots):
            raise ValueError("Shots must refer to distinct decoded keyframes")
        cursor = 0
        for shot in self.shots:
            if shot.start_tick != cursor:
                raise ValueError("Measured shot intervals must be continuous")
            cursor = shot.end_tick
        if cursor != self.duration_ticks:
            raise ValueError("Shot intervals must cover exactly the measured decoded duration")
        if self.duration_ticks * self.timebase_numerator / self.timebase_denominator > 600:
            raise ValueError("Measured video exceeds the indexing duration budget")
        return self


class IndexMediaRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    retry_failed: bool = False


class SearchShotsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    item_ids: list[uuid.UUID] = Field(min_length=1, max_length=8)
    query: str = Field(min_length=1, max_length=160)
    limit: int = Field(default=20, ge=1, le=50)


class ReconcileIndexRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    confirmed_worker_stopped: bool
