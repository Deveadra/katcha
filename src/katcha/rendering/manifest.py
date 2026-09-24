from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator

from katcha.audio.captions import build_caption_cues
from katcha.rendering.reactions import (
    ReactionAssetPack,
    ReactionCue,
    ReactionEvent,
    resolve_reaction_events,
)


class RenderCaptionCue(BaseModel):
    start_seconds: float = Field(ge=0)
    end_seconds: float = Field(gt=0)
    text: str


class NarrationOverlay(BaseModel):
    asset_key: str
    placement: Literal["pre", "mid", "post"]
    text: str
    start_seconds: float = Field(ge=0)
    duration_seconds: float = Field(gt=0)
    source_time_seconds: float | None = Field(default=None, ge=0)
    cues: list[RenderCaptionCue] = Field(default_factory=list)


class SourceVideoSpec(BaseModel):
    storage_key: str
    duration_seconds: float = Field(gt=0)
    width: int | None = None
    height: int | None = None
    audio_volume: float = Field(default=0.45, ge=0, le=1)


class BrandPalette(BaseModel):
    ink: str = "#101216"
    paper: str = "#F6F3EC"
    signal_blue: str = "#5B6CFF"
    hot_peach: str = "#FF7657"
    volt: str = "#D9FF57"


class CaptionBrandSpec(BaseModel):
    treatment_key: str = "impact_clean_v1"
    font_family: str = "Arial, Helvetica, sans-serif"
    font_size_px: int = Field(default=66, ge=32, le=120)
    font_weight: int = Field(default=900, ge=400, le=1000)
    max_visual_lines: int = Field(default=2, ge=1, le=3)
    bottom_safe_zone_px: int = Field(default=250, ge=120, le=600)


class MotionBrandSpec(BaseModel):
    treatment_key: str = "restrained_punch_v1"
    max_punch_scale: float = Field(default=1.08, ge=1.0, le=1.2)
    freeze_frame_max_frames: int = Field(default=8, ge=0, le=30)
    random_motion_enabled: bool = False


class EndCardBrandSpec(BaseModel):
    treatment_key: str = "verdict_v1"
    accent_role: Literal["signal_blue", "hot_peach", "volt"] = "signal_blue"
    max_question_lines: int = Field(default=3, ge=1, le=4)
    label: str | None = Field(default=None, max_length=80)


class ShortBrandSpec(BaseModel):
    brand_key: str = "channel_01"
    version: int = Field(default=1, ge=1)
    theme_key: str = "signal_v1"
    palette: BrandPalette = Field(default_factory=BrandPalette)
    captions: CaptionBrandSpec = Field(default_factory=CaptionBrandSpec)
    motion: MotionBrandSpec = Field(default_factory=MotionBrandSpec)
    end_card: EndCardBrandSpec = Field(default_factory=EndCardBrandSpec)

    @model_validator(mode="after")
    def resolve_legacy_end_card_label(self) -> ShortBrandSpec:
        if not (self.end_card.label or "").strip():
            self.end_card.label = (
                "RankSnaxx ruling" if self.brand_key == "ranksnaxx" else "Your ruling"
            )
        return self


def channel_01_brand_v1() -> ShortBrandSpec:
    return ShortBrandSpec()


class ShortRenderManifest(BaseModel):
    version: Literal["short-render-v1"] = "short-render-v1"
    production_id: str
    width: int = 1080
    height: int = 1920
    fps: int = 30
    source: SourceVideoSpec
    overlays: list[NarrationOverlay] = Field(default_factory=list)
    output_duration_seconds: float = Field(gt=0)
    output_key: str
    title_angle: str | None = None
    interaction_prompt: str | None = None
    brand: ShortBrandSpec = Field(default_factory=channel_01_brand_v1)
    reaction_events: list[ReactionEvent] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_reaction_events(self) -> ShortRenderManifest:
        ids: set[str] = set()
        for event in self.reaction_events:
            if event.id in ids:
                raise ValueError(f"duplicate reaction event id: {event.id}")
            ids.add(event.id)
            if event.brand_key != self.brand.brand_key:
                raise ValueError("reaction event belongs to a different channel")
            if event.start_seconds + event.duration_seconds > (
                self.output_duration_seconds + 0.00001
            ):
                raise ValueError("reaction event extends beyond render")
        return self


def build_short_manifest(
    *,
    production_id: str,
    source_key: str,
    source_duration_seconds: float,
    source_width: int | None,
    source_height: int | None,
    source_audio_volume: float,
    width: int,
    height: int,
    fps: int,
    script_segments: list[dict[str, object]],
    narration_assets: list[dict[str, object]],
    output_key: str,
    title_angle: str | None,
    interaction_prompt: str | None,
    brand: ShortBrandSpec | None = None,
    reaction_pack: ReactionAssetPack | None = None,
    reaction_cues: list[ReactionCue | dict[str, object]] | None = None,
) -> ShortRenderManifest:
    assets_by_index = {
        int(asset["segment_index"]): asset
        for asset in narration_assets
        if asset.get("segment_index") is not None
    }
    overlays: list[NarrationOverlay] = []
    narration_cursor = 0.15

    for index, segment in enumerate(script_segments):
        asset = assets_by_index.get(index)
        if asset is None:
            raise ValueError(f"missing narration asset for segment {index}")
        placement = str(segment.get("placement") or "")
        duration = float(asset["duration_seconds"])
        source_time = segment.get("source_time_seconds")
        source_time_value = float(source_time) if source_time is not None else None

        if placement == "pre":
            requested_start = 0.15
        elif placement == "mid":
            if source_time_value is None:
                raise ValueError("mid narration segment requires source_time_seconds")
            requested_start = source_time_value
        elif placement == "post":
            requested_start = source_duration_seconds + 0.1
        else:
            raise ValueError(f"unsupported commentary placement: {placement}")

        start = max(requested_start, narration_cursor)
        text = str(segment.get("text") or "").strip()
        cues = [
            RenderCaptionCue(
                start_seconds=cue.start_seconds,
                end_seconds=cue.end_seconds,
                text=cue.text,
            )
            for cue in build_caption_cues(
                text,
                duration_seconds=duration,
                start_offset_seconds=start,
            )
        ]
        overlays.append(
            NarrationOverlay(
                asset_key=str(asset["storage_key"]),
                placement=placement,
                text=text,
                start_seconds=round(start, 3),
                duration_seconds=round(duration, 3),
                source_time_seconds=source_time_value,
                cues=cues,
            )
        )
        narration_cursor = start + duration + 0.08

    output_duration = max(source_duration_seconds, narration_cursor) + 0.2
    selected_brand = brand or channel_01_brand_v1()
    reaction_events = resolve_reaction_events(
        cues=reaction_cues,
        pack=reaction_pack,
        brand_key=selected_brand.brand_key,
        narration_windows={
            index: (overlay.start_seconds, overlay.duration_seconds)
            for index, overlay in enumerate(overlays)
        },
        output_duration_seconds=round(output_duration, 3),
    )
    return ShortRenderManifest(
        production_id=production_id,
        width=width,
        height=height,
        fps=fps,
        source=SourceVideoSpec(
            storage_key=source_key,
            duration_seconds=source_duration_seconds,
            width=source_width,
            height=source_height,
            audio_volume=source_audio_volume,
        ),
        overlays=overlays,
        output_duration_seconds=round(output_duration, 3),
        output_key=output_key,
        title_angle=title_angle,
        interaction_prompt=interaction_prompt,
        brand=selected_brand,
        reaction_events=reaction_events,
    )
