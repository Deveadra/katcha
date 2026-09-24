from __future__ import annotations

from copy import deepcopy
from typing import Literal

from pydantic import BaseModel, Field, model_validator


class SourceLayoutPolicy(BaseModel):
    mode: Literal["full_frame", "header_panel"]
    fit: Literal["contain", "cover"] = "contain"
    background_mode: Literal["solid", "blurred_fill"] = "solid"
    header_height_px: int = Field(default=0, ge=0, le=700)


class NarrationPolicy(BaseModel):
    mode: Literal["persona_voice", "explanatory_voice", "text_only", "source_only"]
    required: bool = False
    captions_enabled: bool = True
    source_audio_policy: Literal["retain", "duck", "mute"] = "duck"
    source_audio_volume: float = Field(default=0.45, ge=0, le=1)
    narration_duck_volume: float = Field(default=0.16, ge=0, le=1)


class HeaderPolicy(BaseModel):
    required: bool = False
    max_chars: int = Field(default=0, ge=0, le=400)
    background: str = "#000000"
    foreground: str = "#FFFFFF"
    font_size_px: int = Field(default=54, ge=24, le=120)
    font_weight: int = Field(default=850, ge=400, le=1000)
    horizontal_padding_px: int = Field(default=56, ge=0, le=180)


class EditingQualityPolicy(BaseModel):
    min_source_seconds: float = Field(default=1.0, gt=0, le=30)
    max_duration_seconds: float = Field(default=60.0, gt=0, le=180)
    max_narration_ratio: float = Field(default=0.55, ge=0, le=1)


class EditBlueprintContract(BaseModel):
    key: str = Field(min_length=1, max_length=96)
    version: str = Field(min_length=1, max_length=32)
    composition: Literal["blueprint_video"] = "blueprint_video"
    source_layout: SourceLayoutPolicy
    narration: NarrationPolicy
    header: HeaderPolicy = Field(default_factory=HeaderPolicy)
    transition: Literal["cut", "punch_cut"] = "cut"
    quality: EditingQualityPolicy = Field(default_factory=EditingQualityPolicy)

    @model_validator(mode="after")
    def validate_contract(self) -> EditBlueprintContract:
        if self.source_layout.mode == "header_panel":
            if self.source_layout.header_height_px <= 0:
                raise ValueError("header-panel blueprint requires a positive header height")
            if not self.header.required:
                raise ValueError("header-panel blueprint must require header copy")
            if self.header.max_chars <= 0:
                raise ValueError("header-panel blueprint requires a positive header character cap")
        elif self.source_layout.header_height_px != 0:
            raise ValueError("full-frame blueprint cannot reserve header height")

        voice_mode = self.narration.mode in {"persona_voice", "explanatory_voice"}
        if self.narration.required and not voice_mode:
            raise ValueError("required narration is only valid for a voice narration mode")
        if not voice_mode and self.narration.captions_enabled:
            raise ValueError("text/source-only blueprints cannot enable narration captions")
        return self


_PERSONA_COMMENTARY_V1 = EditBlueprintContract(
    key="persona_commentary",
    version="1.0.0",
    source_layout=SourceLayoutPolicy(
        mode="full_frame",
        fit="contain",
        background_mode="blurred_fill",
        header_height_px=0,
    ),
    narration=NarrationPolicy(
        mode="persona_voice",
        required=True,
        captions_enabled=True,
        source_audio_policy="duck",
        source_audio_volume=0.35,
        narration_duck_volume=0.14,
    ),
    header=HeaderPolicy(required=False, max_chars=0),
    transition="punch_cut",
    quality=EditingQualityPolicy(
        min_source_seconds=2.0,
        max_duration_seconds=60.0,
        max_narration_ratio=0.48,
    ),
)

_HEADER_EXPLAINER_V1 = EditBlueprintContract(
    key="header_explainer",
    version="1.0.0",
    source_layout=SourceLayoutPolicy(
        mode="header_panel",
        fit="contain",
        background_mode="solid",
        header_height_px=360,
    ),
    narration=NarrationPolicy(
        mode="text_only",
        required=False,
        captions_enabled=False,
        source_audio_policy="retain",
        source_audio_volume=0.72,
        narration_duck_volume=0.18,
    ),
    header=HeaderPolicy(
        required=True,
        max_chars=220,
        background="#000000",
        foreground="#FFFFFF",
        font_size_px=52,
        font_weight=850,
        horizontal_padding_px=56,
    ),
    transition="cut",
    quality=EditingQualityPolicy(
        min_source_seconds=2.0,
        max_duration_seconds=60.0,
        max_narration_ratio=0.0,
    ),
)

_REGISTRY = {
    (_PERSONA_COMMENTARY_V1.key, _PERSONA_COMMENTARY_V1.version): _PERSONA_COMMENTARY_V1,
    (_HEADER_EXPLAINER_V1.key, _HEADER_EXPLAINER_V1.version): _HEADER_EXPLAINER_V1,
}


def persona_commentary_v1() -> EditBlueprintContract:
    return EditBlueprintContract.model_validate(
        deepcopy(_PERSONA_COMMENTARY_V1.model_dump(mode="json"))
    )


def header_explainer_v1() -> EditBlueprintContract:
    return EditBlueprintContract.model_validate(
        deepcopy(_HEADER_EXPLAINER_V1.model_dump(mode="json"))
    )


def get_edit_blueprint(key: str, version: str) -> EditBlueprintContract:
    try:
        blueprint = _REGISTRY[(key, version)]
    except KeyError as exc:
        raise KeyError(f"unknown edit blueprint: {key}@{version}") from exc
    return EditBlueprintContract.model_validate(
        deepcopy(blueprint.model_dump(mode="json"))
    )
