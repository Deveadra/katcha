from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from katcha.audio.captions import build_caption_cues


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
    )
