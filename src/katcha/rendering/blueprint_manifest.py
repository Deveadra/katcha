from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator

from katcha.audio.captions import build_caption_cues
from katcha.editing.blueprints import EditBlueprintContract
from katcha.rendering.manifest import RenderCaptionCue, ShortBrandSpec


class BlueprintHeaderPlan(BaseModel):
    enabled: bool
    height_px: int = Field(ge=0, le=700)
    text: str | None = Field(default=None, max_length=400)
    background: str = "#000000"
    foreground: str = "#FFFFFF"
    font_size_px: int = Field(default=54, ge=24, le=120)
    font_weight: int = Field(default=850, ge=400, le=1000)
    horizontal_padding_px: int = Field(default=56, ge=0, le=180)


class BlueprintSourcePlan(BaseModel):
    storage_key: str = Field(min_length=1)
    duration_seconds: float = Field(gt=0)
    source_start_seconds: float = Field(default=0, ge=0)
    source_end_seconds: float = Field(gt=0)
    top_px: int = Field(default=0, ge=0, le=700)
    fit: Literal["contain", "cover"] = "contain"
    background_mode: Literal["solid", "blurred_fill"] = "solid"
    native_audio_policy: Literal["retain", "duck", "mute"]
    audio_volume: float = Field(ge=0, le=1)

    @model_validator(mode="after")
    def validate_trim(self) -> BlueprintSourcePlan:
        if self.source_end_seconds <= self.source_start_seconds:
            raise ValueError("source trim end must be after trim start")
        trimmed = self.source_end_seconds - self.source_start_seconds
        if abs(trimmed - self.duration_seconds) > 0.01:
            raise ValueError("source duration must match trim range")
        return self


class BlueprintNarrationPlan(BaseModel):
    asset_key: str = Field(min_length=1)
    text: str = Field(min_length=1, max_length=1200)
    start_seconds: float = Field(default=0.15, ge=0)
    duration_seconds: float = Field(gt=0)
    cues: list[RenderCaptionCue] = Field(default_factory=list)


class BlueprintRenderManifest(BaseModel):
    version: Literal["blueprint-render-v1"] = "blueprint-render-v1"
    render_id: str = Field(min_length=1)
    channel_profile_id: str = Field(min_length=1)
    brand_key: str = Field(min_length=1)
    brand_version: int = Field(ge=1)
    blueprint_key: str = Field(min_length=1)
    blueprint_version: str = Field(min_length=1)
    blueprint_snapshot: dict[str, object]
    brand: ShortBrandSpec
    width: int = Field(default=1080, ge=320, le=3840)
    height: int = Field(default=1920, ge=320, le=3840)
    fps: int = Field(default=30, ge=1, le=120)
    source: BlueprintSourcePlan
    header: BlueprintHeaderPlan
    narration: BlueprintNarrationPlan | None = None
    output_duration_seconds: float = Field(gt=0)
    output_key: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_frozen_lineage(self) -> BlueprintRenderManifest:
        blueprint = EditBlueprintContract.model_validate(self.blueprint_snapshot)
        if blueprint.key != self.blueprint_key or blueprint.version != self.blueprint_version:
            raise ValueError("blueprint snapshot identity does not match manifest lineage")
        if self.brand.brand_key != self.brand_key or self.brand.version != self.brand_version:
            raise ValueError("brand snapshot identity does not match manifest lineage")

        expects_header = blueprint.source_layout.mode == "header_panel"
        if self.header.enabled != expects_header:
            raise ValueError("compiled header state does not match frozen blueprint")
        expected_top = blueprint.source_layout.header_height_px if expects_header else 0
        if self.source.top_px != expected_top:
            raise ValueError("compiled source position does not match frozen blueprint")
        if expects_header and not (self.header.text or "").strip():
            raise ValueError("header-panel render is missing required explanatory copy")

        if blueprint.narration.required and self.narration is None:
            raise ValueError("blueprint requires narration")
        if blueprint.narration.mode in {"text_only", "source_only"} and self.narration is not None:
            raise ValueError("blueprint forbids generated narration audio")
        if self.output_duration_seconds > blueprint.quality.max_duration_seconds + 0.00001:
            raise ValueError("render exceeds blueprint maximum duration")
        if self.narration is not None:
            narration_end = self.narration.start_seconds + self.narration.duration_seconds
            if narration_end > self.output_duration_seconds + 0.00001:
                raise ValueError("narration extends beyond render duration")
            ratio = self.narration.duration_seconds / self.output_duration_seconds
            if ratio > blueprint.quality.max_narration_ratio + 0.00001:
                raise ValueError("narration density exceeds blueprint quality policy")
        return self


def build_blueprint_render_manifest(
    *,
    render_id: str,
    channel_profile_id: str,
    brand: ShortBrandSpec,
    blueprint: EditBlueprintContract,
    source_storage_key: str,
    source_duration_seconds: float,
    output_key: str,
    headline: str | None = None,
    narration_asset_key: str | None = None,
    narration_text: str | None = None,
    narration_duration_seconds: float | None = None,
    width: int = 1080,
    height: int = 1920,
    fps: int = 30,
) -> BlueprintRenderManifest:
    if source_duration_seconds < blueprint.quality.min_source_seconds:
        raise ValueError("source is shorter than the blueprint minimum")
    duration = min(source_duration_seconds, blueprint.quality.max_duration_seconds)

    normalized_headline = (headline or "").strip()
    if blueprint.header.required:
        if not normalized_headline:
            raise ValueError("blueprint requires explanatory header copy")
        if len(normalized_headline) > blueprint.header.max_chars:
            raise ValueError("header copy exceeds blueprint character cap")
    elif normalized_headline:
        raise ValueError("full-frame blueprint does not accept persistent header copy")

    narration_fields = (
        narration_asset_key,
        narration_text,
        narration_duration_seconds,
    )
    has_any_narration = any(value is not None for value in narration_fields)
    has_all_narration = all(value is not None for value in narration_fields)
    if has_any_narration and not has_all_narration:
        raise ValueError("narration asset, text and duration must be provided together")
    if blueprint.narration.required and not has_all_narration:
        raise ValueError("blueprint requires narration")
    if blueprint.narration.mode in {"text_only", "source_only"} and has_any_narration:
        raise ValueError("blueprint does not permit generated narration audio")

    narration: BlueprintNarrationPlan | None = None
    if has_all_narration:
        narration_duration = float(narration_duration_seconds or 0)
        if narration_duration <= 0:
            raise ValueError("narration duration must be positive")
        narration_start = 0.15
        if narration_start + narration_duration > duration:
            raise ValueError("narration does not fit inside source duration")
        cues = []
        if blueprint.narration.captions_enabled:
            cues = [
                RenderCaptionCue(
                    start_seconds=cue.start_seconds,
                    end_seconds=cue.end_seconds,
                    text=cue.text,
                )
                for cue in build_caption_cues(
                    str(narration_text),
                    duration_seconds=narration_duration,
                    start_offset_seconds=narration_start,
                )
            ]
        narration = BlueprintNarrationPlan(
            asset_key=str(narration_asset_key),
            text=str(narration_text).strip(),
            start_seconds=narration_start,
            duration_seconds=narration_duration,
            cues=cues,
        )

    if blueprint.narration.source_audio_policy == "mute":
        source_volume = 0.0
    elif blueprint.narration.source_audio_policy == "duck" and narration is not None:
        source_volume = blueprint.narration.narration_duck_volume
    else:
        source_volume = blueprint.narration.source_audio_volume

    header_enabled = blueprint.source_layout.mode == "header_panel"
    header = BlueprintHeaderPlan(
        enabled=header_enabled,
        height_px=blueprint.source_layout.header_height_px if header_enabled else 0,
        text=normalized_headline or None,
        background=blueprint.header.background,
        foreground=blueprint.header.foreground,
        font_size_px=blueprint.header.font_size_px,
        font_weight=blueprint.header.font_weight,
        horizontal_padding_px=blueprint.header.horizontal_padding_px,
    )
    source = BlueprintSourcePlan(
        storage_key=source_storage_key,
        duration_seconds=round(duration, 3),
        source_start_seconds=0.0,
        source_end_seconds=round(duration, 3),
        top_px=header.height_px,
        fit=blueprint.source_layout.fit,
        background_mode=blueprint.source_layout.background_mode,
        native_audio_policy=blueprint.narration.source_audio_policy,
        audio_volume=round(source_volume, 4),
    )

    return BlueprintRenderManifest(
        render_id=render_id,
        channel_profile_id=channel_profile_id,
        brand_key=brand.brand_key,
        brand_version=brand.version,
        blueprint_key=blueprint.key,
        blueprint_version=blueprint.version,
        blueprint_snapshot=blueprint.model_dump(mode="json"),
        brand=brand,
        width=width,
        height=height,
        fps=fps,
        source=source,
        header=header,
        narration=narration,
        output_duration_seconds=round(duration, 3),
        output_key=output_key,
    )
