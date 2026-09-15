from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator

from katcha.audio.captions import build_caption_cues


class LongformCaptionCue(BaseModel):
    start_seconds: float = Field(ge=0)
    end_seconds: float = Field(gt=0)
    text: str


class LongformClipSpec(BaseModel):
    clip_id: str
    storage_key: str
    source_duration_seconds: float = Field(gt=0)
    source_start_seconds: float = Field(default=0, ge=0)
    source_end_seconds: float = Field(gt=0)
    width: int | None = None
    height: int | None = None
    audio_volume: float = Field(default=0.45, ge=0, le=1)
    transition_text: str | None = None


class LongformNarrationSpec(BaseModel):
    asset_key: str
    text: str
    background_key: str | None = None
    background_source_start_seconds: float = Field(default=0, ge=0)
    cues: list[LongformCaptionCue] = Field(default_factory=list)


class LongformTimelineItem(BaseModel):
    kind: Literal["clip", "narration"]
    start_seconds: float = Field(ge=0)
    duration_seconds: float = Field(gt=0)
    clip: LongformClipSpec | None = None
    narration: LongformNarrationSpec | None = None

    @model_validator(mode="after")
    def validate_payload(self) -> LongformTimelineItem:
        if self.kind == "clip" and (self.clip is None or self.narration is not None):
            raise ValueError("clip timeline item requires only clip payload")
        if self.kind == "narration" and (
            self.narration is None or self.clip is not None
        ):
            raise ValueError("narration timeline item requires only narration payload")
        return self


class LongformRenderManifest(BaseModel):
    version: Literal["longform-render-v1"] = "longform-render-v1"
    compilation_id: str
    width: int = 1920
    height: int = 1080
    fps: int = 30
    title_angle: str
    opening_hook: str
    timeline: list[LongformTimelineItem] = Field(min_length=1)
    output_duration_seconds: float = Field(gt=0)
    output_key: str


def _narration_item(
    *,
    role: str,
    text: str | None,
    cursor: float,
    narration_assets: dict[str, dict[str, object]],
    background_key: str | None,
    background_source_start_seconds: float,
) -> LongformTimelineItem | None:
    clean = (text or "").strip()
    if not clean:
        return None
    asset = narration_assets.get(role)
    if asset is None:
        raise ValueError(f"missing long-form narration asset: {role}")
    duration = float(asset.get("duration_seconds") or 0)
    if duration <= 0:
        raise ValueError(f"invalid narration duration for role: {role}")
    cues = [
        LongformCaptionCue(
            start_seconds=cue.start_seconds,
            end_seconds=cue.end_seconds,
            text=cue.text,
        )
        for cue in build_caption_cues(
            clean,
            duration_seconds=duration,
            start_offset_seconds=cursor,
        )
    ]
    return LongformTimelineItem(
        kind="narration",
        start_seconds=round(cursor, 3),
        duration_seconds=round(duration, 3),
        narration=LongformNarrationSpec(
            asset_key=str(asset["storage_key"]),
            text=clean,
            background_key=background_key,
            background_source_start_seconds=max(0, background_source_start_seconds),
            cues=cues,
        ),
    )


def build_longform_manifest(
    *,
    compilation_id: str,
    title_angle: str,
    opening_hook: str,
    intro: str | None,
    outro: str | None,
    segments: list[dict[str, object]],
    narration_assets: dict[str, dict[str, object]],
    source_audio_volume: float,
    width: int,
    height: int,
    fps: int,
    output_key: str,
) -> LongformRenderManifest:
    if not segments:
        raise ValueError("long-form manifest requires at least one segment")

    timeline: list[LongformTimelineItem] = []
    cursor = 0.0
    gap = 0.06
    first = segments[0]
    first_key = str(first["source_key"])
    first_start = float(first.get("source_start_seconds") or 0)

    for role, text in (("opening", opening_hook), ("intro", intro)):
        item = _narration_item(
            role=role,
            text=text,
            cursor=cursor,
            narration_assets=narration_assets,
            background_key=first_key,
            background_source_start_seconds=first_start,
        )
        if item is not None:
            timeline.append(item)
            cursor += item.duration_seconds + gap

    for position, segment in enumerate(segments):
        source_key = str(segment["source_key"])
        source_duration = float(segment["source_duration_seconds"])
        start = float(segment.get("source_start_seconds") or 0)
        end = float(segment.get("source_end_seconds") or source_duration)
        if start < 0 or end <= start or end > source_duration + 0.05:
            raise ValueError(f"invalid source range for long-form segment {position}")

        before = _narration_item(
            role=f"seg:{position}:before",
            text=str(segment.get("host_before") or "") or None,
            cursor=cursor,
            narration_assets=narration_assets,
            background_key=source_key,
            background_source_start_seconds=start,
        )
        if before is not None:
            timeline.append(before)
            cursor += before.duration_seconds + gap

        clip_duration = end - start
        timeline.append(
            LongformTimelineItem(
                kind="clip",
                start_seconds=round(cursor, 3),
                duration_seconds=round(clip_duration, 3),
                clip=LongformClipSpec(
                    clip_id=str(segment["clip_id"]),
                    storage_key=source_key,
                    source_duration_seconds=source_duration,
                    source_start_seconds=start,
                    source_end_seconds=end,
                    width=(int(segment["source_width"]) if segment.get("source_width") else None),
                    height=(
                        int(segment["source_height"]) if segment.get("source_height") else None
                    ),
                    audio_volume=source_audio_volume,
                    transition_text=(
                        str(segment.get("transition_before") or "").strip() or None
                    ),
                ),
            )
        )
        cursor += clip_duration + gap

        after = _narration_item(
            role=f"seg:{position}:after",
            text=str(segment.get("host_after") or "") or None,
            cursor=cursor,
            narration_assets=narration_assets,
            background_key=source_key,
            background_source_start_seconds=max(start, end - 1.0),
        )
        if after is not None:
            timeline.append(after)
            cursor += after.duration_seconds + gap

    last = segments[-1]
    outro_item = _narration_item(
        role="outro",
        text=outro,
        cursor=cursor,
        narration_assets=narration_assets,
        background_key=str(last["source_key"]),
        background_source_start_seconds=max(
            float(last.get("source_start_seconds") or 0),
            float(last.get("source_end_seconds") or last["source_duration_seconds"]) - 1.0,
        ),
    )
    if outro_item is not None:
        timeline.append(outro_item)
        cursor += outro_item.duration_seconds + gap

    return LongformRenderManifest(
        compilation_id=compilation_id,
        width=width,
        height=height,
        fps=fps,
        title_angle=title_angle,
        opening_hook=opening_hook,
        timeline=timeline,
        output_duration_seconds=round(max(cursor, 0.1), 3),
        output_key=output_key,
    )
