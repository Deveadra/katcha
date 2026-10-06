"""Versioned visual data contracts. These never contain executable renderer code."""

from typing import Literal, Self
from uuid import UUID

from pydantic import Field, model_serializer, model_validator

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
    crop: Region | None = None

    @model_serializer(mode="wrap")
    def preserve_existing_media_use(self, handler):
        value = handler(self)
        if self.crop is None:
            value.pop("crop", None)
        return value


class VisualOverlay(Contract):
    kind: Literal["circle", "arrow", "highlight"]
    media_index: int = Field(default=0, ge=0, le=1, strict=True)
    region: Region
    label: str | None = Field(default=None, max_length=100)


class VisualBeat(Contract):
    beat_id: Identity
    layout: Literal["single", "comparison", "quote", "image", "image_comparison"]
    media: list[VisualMediaUse] = Field(default_factory=list, max_length=2)
    quote_source_id: Identity | None = None
    image_id: UUID | None = None
    image_ids: list[UUID] = Field(default_factory=list, max_length=2)
    image_push_in: float = Field(default=1, ge=1, le=1.15)
    overlays: list[VisualOverlay] = Field(default_factory=list, max_length=8)
    caption_position: Literal["bottom", "center"] = "bottom"
    caption_scale: float = Field(default=1, ge=0.75, le=1.35)
    caption_background: bool = False
    transition: Literal["cut", "fade"] = "cut"
    transition_frames: int = Field(default=8, ge=3, le=15, strict=True)

    @model_validator(mode="after")
    def coherent_layout(self) -> Self:
        expected = {"single": 1, "comparison": 2, "quote": 0, "image": 0, "image_comparison": 0}[
            self.layout
        ]
        if len(self.media) != expected:
            raise ValueError("Visual layout has the wrong number of media sources")
        if (self.layout == "image") != bool(self.image_id):
            raise ValueError("Only image layouts require an uploaded image")
        if self.layout == "image_comparison":
            if len(self.image_ids) != 2 or len(set(self.image_ids)) != 2:
                raise ValueError("Image comparisons require two distinct uploaded images")
        elif self.image_ids:
            raise ValueError("Only image comparisons may select multiple images")
        if self.layout not in {"image", "image_comparison"} and self.image_push_in != 1:
            raise ValueError("Image motion is only valid for image layouts")
        if (self.layout == "quote") != bool(self.quote_source_id):
            raise ValueError("Only quote layouts require a research source")
        sources = (
            len(self.image_ids)
            if self.layout == "image_comparison"
            else (1 if self.layout == "image" else len(self.media))
        )
        if any(overlay.media_index >= sources for overlay in self.overlays):
            raise ValueError("Visual annotation references an absent source")
        return self

    @model_serializer(mode="wrap")
    def preserve_existing_visuals(self, handler):
        value = handler(self)
        if self.layout != "image_comparison":
            value.pop("image_ids", None)
        if self.layout not in {"image", "image_comparison"}:
            value.pop("image_id", None)
            value.pop("image_push_in", None)
        if self.caption_position == "bottom":
            value.pop("caption_position", None)
        if self.caption_scale == 1:
            value.pop("caption_scale", None)
        if not self.caption_background:
            value.pop("caption_background", None)
        if self.transition == "cut":
            value.pop("transition", None)
            value.pop("transition_frames", None)
        return value


class RenderImage(Contract):
    image_id: UUID
    beat_id: Identity
    storage_key: str = Field(min_length=1, max_length=1000)
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    width: int = Field(gt=0, le=8192, strict=True)
    height: int = Field(gt=0, le=8192, strict=True)
    title: str = Field(min_length=1, max_length=200)
    illustration: bool

    @model_validator(mode="after")
    def managed_png(self) -> Self:
        if (
            self.width * self.height > 16_000_000
            or not self.storage_key.startswith("editorial/")
            or not self.storage_key.endswith(f"/images/{self.image_id}/{self.sha256}.png")
            or any(part in {".", ".."} for part in self.storage_key.split("/"))
            or any(ord(char) < 32 or char in ":\\" for char in self.storage_key)
        ):
            raise ValueError("Image requires a bounded, managed PNG receipt")
        return self


class StoryboardPlan(Contract):
    # Caption-only is an explicit editorial choice, never a silent substitute for narration.
    presentation_mode: Literal["captioned_silent", "narrated"]
    beats: list[VisualBeat] = Field(min_length=1, max_length=100)
    narration_ids: dict[Identity, UUID] = Field(default_factory=dict, max_length=100)

    @model_validator(mode="after")
    def narration_selection(self) -> Self:
        if self.presentation_mode == "captioned_silent" and self.narration_ids:
            raise ValueError("Silent storyboards cannot select narration")
        if self.presentation_mode == "narrated" and set(self.narration_ids) != {
            beat.beat_id for beat in self.beats
        }:
            raise ValueError("Select one narration recording for every storyboard beat")
        return self


class RenderNarration(Contract):
    narration_id: UUID
    beat_id: Identity
    storage_key: str = Field(min_length=1, max_length=1000)
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    text_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    sample_rate: int = Field(ge=8000, le=96000, strict=True)
    sample_frames: int = Field(gt=0, le=57600000, strict=True)

    @model_validator(mode="after")
    def bounded_audio(self) -> Self:
        if self.sample_frames > self.sample_rate * 600:
            raise ValueError("Narration exceeds ten minutes per beat")
        if (
            not self.storage_key.startswith("editorial/")
            or "/narration/" not in self.storage_key
            or not self.storage_key.endswith(f"/{self.sha256}.wav")
            or any(part in {".", ".."} for part in self.storage_key.split("/"))
            or any(ord(char) < 32 or char in ":\\" for char in self.storage_key)
        ):
            raise ValueError("Narration requires a managed content-addressed WAV key")
        return self


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
    version: Literal[
        "editorial-render-v1", "editorial-render-v2", "editorial-render-v3", "editorial-render-v4"
    ] = "editorial-render-v1"
    project_id: str
    revision: int = Field(gt=0, strict=True)
    draft_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    presentation_mode: Literal["captioned_silent", "narrated"]
    narration: list[RenderNarration] = Field(default_factory=list, max_length=100)
    images: list[RenderImage] = Field(default_factory=list, max_length=100)
    width: Literal[1920] = 1920
    height: Literal[1080] = 1080
    fps: Literal[30] = 30
    media: list[RenderMedia] = Field(default_factory=list, max_length=30)
    timeline: list[EditorialScene] = Field(min_length=1, max_length=100)
    output_duration_seconds: float = Field(gt=0, le=3600)
    output_key: str
    requires_editorial_review: Literal[True] = True

    @model_serializer(mode="wrap")
    def preserve_v1(self, handler):
        value = handler(self)
        if self.version == "editorial-render-v1":
            value.pop("narration", None)
        if self.version not in {"editorial-render-v3", "editorial-render-v4"}:
            value.pop("images", None)
        return value

    @model_validator(mode="after")
    def valid_timeline(self) -> Self:
        narrated = self.presentation_mode == "narrated"
        if self.version not in {"editorial-render-v3", "editorial-render-v4"} and narrated != (
            self.version == "editorial-render-v2"
        ):
            raise ValueError("Narrated rendering requires manifest version 2")
        if not narrated and self.narration:
            raise ValueError("Silent rendering cannot contain narration")
        if narrated and [item.beat_id for item in self.narration] != [
            scene.beat_id for scene in self.timeline
        ]:
            raise ValueError("Narration must cover the timeline once in beat order")
        for scene, audio in zip(self.timeline, self.narration, strict=False):
            frames = (audio.sample_frames * self.fps + audio.sample_rate - 1) // audio.sample_rate
            if scene.duration_frames != frames:
                raise ValueError("Scene duration must follow the measured narration samples")
        images = {item.image_id: item for item in self.images}
        selected_images = {
            identity
            for scene in self.timeline
            for identity in ([scene.image_id] if scene.image_id else scene.image_ids)
        }
        if self.version != "editorial-render-v4" and any(
            scene.layout == "image_comparison" or (scene.layout == "image" and scene.overlays)
            for scene in self.timeline
        ):
            raise ValueError("Image comparisons and annotations require manifest version 4")
        if (
            len(images) != len(self.images)
            or set(images) != selected_images
            or bool(images) != (self.version in {"editorial-render-v3", "editorial-render-v4"})
        ):
            raise ValueError(
                "Image manifests must cover exactly the selected images in version 3 or 4"
            )
        for scene in self.timeline:
            for identity in [scene.image_id] if scene.image_id else scene.image_ids:
                if images[identity].beat_id != scene.beat_id:
                    raise ValueError("Image must belong to its saved script beat")
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


class DirectionOptions(Contract):
    annotate_regions: bool = False

    @model_serializer(mode="wrap")
    def preserve_existing_options(self, handler):
        value = handler(self)
        if not self.annotate_regions:
            value.pop("annotate_regions", None)
        return value

    presentation_mode: Literal["captioned_silent", "narrated"]
    narration_ids: dict[Identity, UUID] = Field(default_factory=dict, max_length=100)

    @model_validator(mode="after")
    def explicit_audio(self) -> Self:
        if self.presentation_mode == "captioned_silent" and self.narration_ids:
            raise ValueError("Silent direction cannot select narration")
        if self.presentation_mode == "narrated" and not self.narration_ids:
            raise ValueError("Choose recordings before directing narrated visuals")
        return self


class DirectedBeat(Contract):
    # Preserve the version-1 provider schema so existing saved responses remain recoverable.
    beat_id: Identity
    layout: Literal["single", "comparison", "quote"]
    media: list[VisualMediaUse] = Field(default_factory=list, max_length=2)
    quote_source_id: Identity | None = None
    overlays: list[VisualOverlay] = Field(default_factory=list, max_length=8)
    rationale: str = Field(min_length=1, max_length=1000)

    @model_validator(mode="after")
    def no_unobserved_regions(self) -> Self:
        # Text-only direction cannot establish where an object appears in a frame.
        VisualBeat.model_validate(self.model_dump(exclude={"rationale"}))
        if self.overlays:
            raise ValueError("Automatic annotations require observed source regions")
        if len({item.candidate_id for item in self.media}) != len(self.media):
            raise ValueError("Automatic comparisons require distinct assets")
        return self


class DirectionResult(Contract):
    beats: list[DirectedBeat] = Field(min_length=1, max_length=100)


class ShotEvidenceReference(Contract):
    beat_id: Identity
    candidate_id: Identity
    observation_id: Identity


class GroundedDirectionResult(DirectionResult):
    shot_evidence: list[ShotEvidenceReference] = Field(default_factory=list, max_length=200)


class StoryboardPreflightRequest(Contract):
    expected_revision: int = Field(gt=0, strict=True)
    asset_run_id: UUID | None = None
    plan: StoryboardPlan
