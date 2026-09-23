from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator

from katcha.audio.captions import build_caption_cues
from katcha.rendering.manifest import RenderCaptionCue, ShortBrandSpec

MAX_RANKED_EPISODE_SECONDS = 60.0
TARGET_SOURCE_SECONDS_BY_COUNT = {3: 8.0, 5: 6.0, 7: 4.25}


class RankedEpisodeSourceSpec(BaseModel):
    clip_id: str
    storage_key: str
    source_start_seconds: float = Field(default=0, ge=0)
    source_end_seconds: float = Field(gt=0)
    duration_seconds: float = Field(gt=0)
    width: int | None = None
    height: int | None = None
    native_audio_policy: Literal["retain", "duck", "mute"] = "duck"
    audio_volume: float = Field(default=0.35, ge=0, le=1)
    narration_duck_volume: float = Field(default=0.16, ge=0, le=1)

    @model_validator(mode="after")
    def validate_trim(self) -> RankedEpisodeSourceSpec:
        if self.source_end_seconds <= self.source_start_seconds:
            raise ValueError("source trim end must be after trim start")
        trimmed = self.source_end_seconds - self.source_start_seconds
        if abs(trimmed - self.duration_seconds) > 0.01:
            raise ValueError("source duration must match its trim range")
        return self


class RankedEpisodeItemSpec(BaseModel):
    position: int = Field(ge=1)
    role: Literal["opener", "build", "false_peak", "payoff"]
    countdown_label: str
    timeline_start_seconds: float = Field(ge=0)
    timeline_end_seconds: float = Field(gt=0)
    transition_before: Literal["cut", "punch_cut", "flash"] = "cut"
    source: RankedEpisodeSourceSpec


class RankedEpisodeNarrationOverlay(BaseModel):
    sequence: int = Field(ge=0)
    asset_key: str
    placement: Literal[
        "opening",
        "reveal",
        "pre_clip",
        "post_clip",
        "transition",
        "closing",
        "interaction",
    ]
    position: int | None = Field(default=None, ge=1)
    clip_id: str | None = None
    text: str
    start_seconds: float = Field(ge=0)
    duration_seconds: float = Field(gt=0)
    cues: list[RenderCaptionCue] = Field(default_factory=list)


class RankedEpisodeEndCard(BaseModel):
    start_seconds: float = Field(ge=0)
    duration_seconds: float = Field(default=2.4, gt=0, le=6)
    prompt: str | None = Field(default=None, max_length=240)


class RankedEpisodeTreatmentMetadata(BaseModel):
    premise: str
    format_key: str
    format_version: str
    item_count: int
    selected_style: str
    ordering_roles: list[str]
    narration_density: float = Field(ge=0)
    brand_key: str
    brand_version: int
    trend_opportunity_id: str | None = None


class RankedEpisodeRenderManifest(BaseModel):
    version: Literal["ranked-episode-render-v1"] = "ranked-episode-render-v1"
    short_episode_id: str
    width: int = 1080
    height: int = 1920
    fps: int = 30
    items: list[RankedEpisodeItemSpec] = Field(min_length=3, max_length=7)
    overlays: list[RankedEpisodeNarrationOverlay] = Field(default_factory=list)
    end_card: RankedEpisodeEndCard
    output_duration_seconds: float = Field(gt=0, le=MAX_RANKED_EPISODE_SECONDS)
    output_key: str
    brand: ShortBrandSpec
    treatment: RankedEpisodeTreatmentMetadata

    @model_validator(mode="after")
    def validate_countdown(self) -> RankedEpisodeRenderManifest:
        positions = [item.position for item in self.items]
        expected = list(range(len(self.items), 0, -1))
        if positions != expected:
            raise ValueError("ranked episode items must be ordered as a descending countdown")
        if self.treatment.item_count != len(self.items):
            raise ValueError("treatment item count must match rendered items")
        return self


def _transition_for(role: str, index: int) -> str:
    if index == 0:
        return "cut"
    if role == "payoff":
        return "flash"
    if role == "false_peak":
        return "punch_cut"
    return "cut"


def _item_window(items: list[RankedEpisodeItemSpec], position: int | None) -> tuple[float, float]:
    if position is None:
        return 0.0, items[-1].timeline_end_seconds
    item = next((value for value in items if value.position == position), None)
    if item is None:
        raise ValueError(f"narration references unknown countdown position: {position}")
    return item.timeline_start_seconds, item.timeline_end_seconds


def _requested_overlay_start(
    *,
    placement: str,
    position: int | None,
    duration: float,
    items: list[RankedEpisodeItemSpec],
    item_timeline_end: float,
    narration_cursor: float,
) -> float:
    item_start, item_end = _item_window(items, position)
    if placement == "opening":
        requested = 0.08
    elif placement in {"reveal", "pre_clip"}:
        requested = item_start + 0.08
    elif placement == "post_clip":
        requested = max(item_start + 0.1, item_end - duration - 0.08)
    elif placement == "transition":
        requested = max(item_start + 0.1, item_end - duration - 0.04)
    elif placement == "closing":
        requested = item_timeline_end + 0.05
    elif placement == "interaction":
        requested = max(item_timeline_end + 0.05, narration_cursor)
    else:
        raise ValueError(f"unsupported episode narration placement: {placement}")
    return max(requested, narration_cursor)


def build_ranked_episode_manifest(
    *,
    short_episode_id: str,
    premise: str,
    format_key: str,
    format_version: str,
    ordered_items: list[dict[str, object]],
    narration_assets: list[dict[str, object]],
    selected_style: str,
    interaction_prompt: str | None,
    output_key: str,
    brand: ShortBrandSpec,
    trend_opportunity_id: str | None = None,
    width: int = 1080,
    height: int = 1920,
    fps: int = 30,
) -> RankedEpisodeRenderManifest:
    item_count = len(ordered_items)
    if item_count not in TARGET_SOURCE_SECONDS_BY_COUNT:
        raise ValueError("ranked episode renderer only supports 3, 5, or 7 items")
    target_source_seconds = TARGET_SOURCE_SECONDS_BY_COUNT[item_count]

    items: list[RankedEpisodeItemSpec] = []
    cursor = 0.0
    for index, raw in enumerate(ordered_items):
        position = int(raw["position"])
        role = str(raw["role"])
        duration = float(raw["source_duration_seconds"])
        if duration <= 0:
            raise ValueError(f"ranked episode clip #{position} has invalid duration")
        trim_duration = min(duration, target_source_seconds)
        source_start = 0.0
        source_end = source_start + trim_duration
        item = RankedEpisodeItemSpec(
            position=position,
            role=role,
            countdown_label=f"#{position}",
            timeline_start_seconds=round(cursor, 3),
            timeline_end_seconds=round(cursor + trim_duration, 3),
            transition_before=_transition_for(role, index),
            source=RankedEpisodeSourceSpec(
                clip_id=str(raw["clip_id"]),
                storage_key=str(raw["storage_key"]),
                source_start_seconds=source_start,
                source_end_seconds=round(source_end, 3),
                duration_seconds=round(trim_duration, 3),
                width=(int(raw["width"]) if raw.get("width") is not None else None),
                height=(int(raw["height"]) if raw.get("height") is not None else None),
                native_audio_policy=str(raw.get("native_audio_policy") or "duck"),
                audio_volume=float(raw.get("audio_volume") or 0.35),
                narration_duck_volume=float(raw.get("narration_duck_volume") or 0.16),
            ),
        )
        items.append(item)
        cursor = item.timeline_end_seconds

    narration_assets = sorted(
        narration_assets,
        key=lambda value: int(value.get("sequence") or 0),
    )
    overlays: list[RankedEpisodeNarrationOverlay] = []
    narration_cursor = 0.08
    narration_seconds = 0.0
    for raw in narration_assets:
        sequence = int(raw["sequence"])
        duration = float(raw["duration_seconds"])
        placement = str(raw["placement"])
        position_raw = raw.get("position")
        position = int(position_raw) if position_raw is not None else None
        start = _requested_overlay_start(
            placement=placement,
            position=position,
            duration=duration,
            items=items,
            item_timeline_end=cursor,
            narration_cursor=narration_cursor,
        )
        text = str(raw.get("text") or "").strip()
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
            RankedEpisodeNarrationOverlay(
                sequence=sequence,
                asset_key=str(raw["storage_key"]),
                placement=placement,
                position=position,
                clip_id=(str(raw["clip_id"]) if raw.get("clip_id") else None),
                text=text,
                start_seconds=round(start, 3),
                duration_seconds=round(duration, 3),
                cues=cues,
            )
        )
        narration_cursor = start + duration + 0.06
        narration_seconds += duration

    interaction = next(
        (overlay for overlay in overlays if overlay.placement == "interaction"),
        None,
    )
    if interaction is not None:
        end_card_start = interaction.start_seconds
        end_card_duration = max(2.4, interaction.duration_seconds + 0.35)
    elif interaction_prompt:
        end_card_start = max(cursor, narration_cursor) + 0.05
        end_card_duration = 2.4
    else:
        end_card_start = max(cursor, narration_cursor)
        end_card_duration = 0.35

    output_duration = max(
        cursor,
        narration_cursor,
        end_card_start + end_card_duration,
    ) + 0.1
    if output_duration > MAX_RANKED_EPISODE_SECONDS:
        raise ValueError(
            "ranked episode exceeds the 60-second Katcha format target; shorten narration "
            "or source treatment before rendering"
        )

    narration_density = narration_seconds / output_duration if output_duration else 0.0
    return RankedEpisodeRenderManifest(
        short_episode_id=short_episode_id,
        width=width,
        height=height,
        fps=fps,
        items=items,
        overlays=overlays,
        end_card=RankedEpisodeEndCard(
            start_seconds=round(end_card_start, 3),
            duration_seconds=round(end_card_duration, 3),
            prompt=interaction_prompt,
        ),
        output_duration_seconds=round(output_duration, 3),
        output_key=output_key,
        brand=brand,
        treatment=RankedEpisodeTreatmentMetadata(
            premise=premise,
            format_key=format_key,
            format_version=format_version,
            item_count=item_count,
            selected_style=selected_style,
            ordering_roles=[item.role for item in items],
            narration_density=round(narration_density, 6),
            brand_key=brand.brand_key,
            brand_version=brand.version,
            trend_opportunity_id=trend_opportunity_id,
        ),
    )
