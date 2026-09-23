from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, Field, model_validator

ReactionAnchor = Literal["top_left", "top_right", "bottom_left", "bottom_right"]
ReactionAnimation = Literal["pop_bounce", "fade", "slide"]


class ReactionAsset(BaseModel):
    storage_key: str = Field(min_length=1)


class ReactionAssetPack(BaseModel):
    """Versioned channel-owned assets; the backing object keys must be write-once."""

    brand_key: str = Field(min_length=1)
    pack_key: str = Field(min_length=1)
    version: int = Field(ge=1)
    assets: dict[str, ReactionAsset] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_paths(self) -> ReactionAssetPack:
        if not re.fullmatch(r"[a-z0-9_-]+", self.brand_key):
            raise ValueError("invalid brand key")
        if not re.fullmatch(r"[a-z0-9_-]+", self.pack_key):
            raise ValueError("invalid reaction pack key")
        prefix = (
            f"brands/{self.brand_key}/reactions/"
            f"{self.pack_key}/v{self.version}/"
        )
        for key, asset in self.assets.items():
            if not re.fullmatch(r"[a-z0-9_-]+", key):
                raise ValueError(f"invalid reaction asset key: {key}")
            if not asset.storage_key.startswith(prefix):
                raise ValueError(f"reaction asset must use versioned channel path: {key}")
            if not asset.storage_key.lower().endswith(".png"):
                raise ValueError(f"reaction asset must be a PNG: {key}")
            if ".." in asset.storage_key or "\\" in asset.storage_key:
                raise ValueError(f"invalid reaction asset path: {key}")
        return self


class ReactionCue(BaseModel):
    """An operator-authored cue relative to an already scheduled narration line."""

    id: str = Field(min_length=1)
    asset_key: str = Field(min_length=1)
    line_ref: int = Field(ge=0)
    offset_seconds: float = Field(default=0, ge=0, allow_inf_nan=False)
    duration_seconds: float = Field(default=1.2, ge=0.2, le=5, allow_inf_nan=False)
    anchor: ReactionAnchor = "bottom_right"
    animation: ReactionAnimation = "pop_bounce"
    scale: float = Field(default=0.22, ge=0.08, le=0.38, allow_inf_nan=False)


class ReactionEvent(ReactionCue):
    brand_key: str
    pack_key: str
    pack_version: int = Field(ge=1)
    storage_key: str
    start_seconds: float = Field(ge=0, allow_inf_nan=False)

    @model_validator(mode="after")
    def validate_frozen_storage(self) -> ReactionEvent:
        prefix = (
            f"brands/{self.brand_key}/reactions/"
            f"{self.pack_key}/v{self.pack_version}/"
        )
        if (
            not self.storage_key.startswith(prefix)
            or not self.storage_key.lower().endswith(".png")
            or ".." in self.storage_key
            or chr(92) in self.storage_key
        ):
            raise ValueError("reaction event has invalid channel-scoped PNG key")
        return self


def resolve_reaction_events(
    *,
    cues: list[ReactionCue | dict[str, object]] | None,
    pack: ReactionAssetPack | None,
    brand_key: str,
    narration_windows: dict[int, tuple[float, float]],
    output_duration_seconds: float,
) -> list[ReactionEvent]:
    """Freeze pack and object keys into render events; never infer cues at render time."""
    if not cues:
        return []
    if pack is None:
        raise ValueError("reaction cues require an asset pack")
    if pack.brand_key != brand_key:
        raise ValueError("reaction asset pack belongs to a different channel")
    events: list[ReactionEvent] = []
    seen: set[str] = set()
    for raw in cues:
        cue = ReactionCue.model_validate(raw)
        if cue.id in seen:
            raise ValueError(f"duplicate reaction cue id: {cue.id}")
        seen.add(cue.id)
        if cue.asset_key not in pack.assets:
            raise ValueError(f"unknown reaction asset: {cue.asset_key}")
        window = narration_windows.get(cue.line_ref)
        if window is None:
            raise ValueError(f"unknown narration line_ref: {cue.line_ref}")
        line_start, line_duration = window
        if cue.offset_seconds > line_duration:
            raise ValueError(f"reaction cue begins after narration line: {cue.id}")
        start = round(line_start + cue.offset_seconds, 3)
        end = start + cue.duration_seconds
        if end > output_duration_seconds + 0.00001:
            raise ValueError(f"reaction cue extends beyond render: {cue.id}")
        events.append(
            ReactionEvent(
                **cue.model_dump(),
                brand_key=pack.brand_key,
                pack_key=pack.pack_key,
                pack_version=pack.version,
                storage_key=pack.assets[cue.asset_key].storage_key,
                start_seconds=start,
            )
        )
    return events
