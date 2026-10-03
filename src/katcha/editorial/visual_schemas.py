"""Versioned visual data contracts. These never contain executable renderer code."""

from typing import Literal, Self
from uuid import UUID

from pydantic import Field, model_validator

from katcha.editorial.project_schemas import Contract, Identity, Text


class Region(Contract):
    x: float = Field(ge=0, le=1)
    y: float = Field(ge=0, le=1)
    width: float = Field(gt=0, le=1)
    height: float = Field(gt=0, le=1)

    @model_validator(mode="after")
    def within_source(self) -> Self:
        if self.x + self.width > 1 or self.y + self.height > 1:
            raise ValueError("Visual region must lie within the source image")
        return self


class VisualMediaUse(Contract):
    candidate_id: Identity
    start_seconds: float = Field(default=0, ge=0)
    playback_rate: float = Field(default=1, ge=0.25, le=2)
    freeze: bool = False
    push_in: float = Field(default=1, ge=1, le=1.15)


class VisualOverlay(Contract):
    kind: Literal["circle", "arrow", "highlight"]
    media_index: int = Field(default=0, ge=0, le=1, strict=True)
    region: Region
    label: str | None = Field(default=None, max_length=100)


class VisualBeat(Contract):
    beat_id: Identity
    layout: Literal["single", "comparison", "quote"]
    media: list[VisualMediaUse] = Field(default_factory=list, max_length=2)
    quote_source_id: Identity | None = None
    overlays: list[VisualOverlay] = Field(default_factory=list, max_length=8)

    @model_validator(mode="after")
    def coherent_layout(self) -> Self:
        expected = {"single": 1, "comparison": 2, "quote": 0}[self.layout]
        if len(self.media) != expected:
            raise ValueError("Visual layout has the wrong number of media sources")
        if (self.layout == "quote") != bool(self.quote_source_id):
            raise ValueError("Only quote layouts require a research source")
        if any(overlay.media_index >= len(self.media) for overlay in self.overlays):
            raise ValueError("Visual annotation references an absent source")
        return self


class StoryboardPlan(Contract):
    # Caption-only is an explicit editorial choice, never a silent substitute for narration.
    presentation_mode: Literal["captioned_silent"]
    beats: list[VisualBeat] = Field(min_length=1, max_length=100)


class RenderMedia(Contract):
    candidate_id: Identity
    clip_id: str
    storage_key: str = Field(min_length=1, max_length=1000)
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    width: int = Field(gt=0, le=16384)
    height: int = Field(gt=0, le=16384)
    duration_seconds: float = Field(gt=0)
    rights_assessment_id: str

    @model_validator(mode="after")
    def managed_key(self) -> Self:
        if (
            ":" in self.storage_key
            or self.storage_key.startswith("/")
            or ".." in self.storage_key.split("/")
        ):
            raise ValueError("Render media must use a managed object key")
        return self


class RenderCaption(Contract):
    start_frame: int = Field(ge=0, strict=True)
    duration_frames: int = Field(gt=0, strict=True)
    text: str = Field(min_length=1, max_length=300)


class EditorialScene(VisualBeat):
    start_frame: int = Field(ge=0, strict=True)
    duration_frames: int = Field(gt=0, strict=True)
    captions: list[RenderCaption] = Field(min_length=1, max_length=100)
    uncertainty_disclosure: Text | None = None
    quote_text: str | None = Field(default=None, max_length=300)
    source_credit: Text | None = None


class EditorialRenderManifest(Contract):
    version: Literal["editorial-render-v1"] = "editorial-render-v1"
    project_id: str
    revision: int = Field(gt=0, strict=True)
    draft_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    presentation_mode: Literal["captioned_silent"]
    width: Literal[1920] = 1920
    height: Literal[1080] = 1080
    fps: Literal[30] = 30
    media: list[RenderMedia] = Field(default_factory=list, max_length=30)
    timeline: list[EditorialScene] = Field(min_length=1, max_length=100)
    output_duration_seconds: float = Field(gt=0, le=3600)
    output_key: str
    requires_editorial_review: Literal[True] = True

    @model_validator(mode="after")
    def valid_timeline(self) -> Self:
        media = {item.candidate_id: item for item in self.media}
        if len(media) != len(self.media):
            raise ValueError("Render media identities must be unique")
        cursor = 0
        for scene in self.timeline:
            if scene.start_frame != cursor:
                raise ValueError("Visual timeline must have continuous frame coverage")
            for use in scene.media:
                asset = media.get(use.candidate_id)
                if asset is None:
                    raise ValueError("Visual scene references missing media")
                end = use.start_seconds + (
                    0 if use.freeze else scene.duration_frames / self.fps * use.playback_rate
                )
                if (
                    use.start_seconds >= asset.duration_seconds
                    or end > asset.duration_seconds + 1e-6
                ):
                    raise ValueError("Visual playback exceeds measured source bounds")
            captions = scene.captions
            caption_cursor = 0
            for caption in captions:
                if caption.start_frame != caption_cursor:
                    raise ValueError("Caption timing must be contiguous within each scene")
                caption_cursor += caption.duration_frames
            if caption_cursor != scene.duration_frames:
                raise ValueError("Captions must cover exactly their scene")
            cursor += scene.duration_frames
        if abs(cursor / self.fps - self.output_duration_seconds) > 1e-6:
            raise ValueError("Output duration must match the integer-frame timeline")
        return self


class StoryboardPreflightRequest(Contract):
    expected_revision: int = Field(gt=0, strict=True)
    asset_run_id: UUID
    plan: StoryboardPlan
