from __future__ import annotations

from copy import deepcopy
from typing import Any

from pydantic import BaseModel, Field


class PersonaReference(BaseModel):
    key: str
    version: str


class VoicePolicy(BaseModel):
    direction_key: str
    preferred_profiles: list[str] = Field(min_length=1)


class ChannelBrandContract(BaseModel):
    brand_key: str
    version: int = Field(ge=1)
    persona: PersonaReference
    voice_policy: VoicePolicy
    visual: dict[str, Any]
    packaging: dict[str, Any]
    interaction: dict[str, Any]
    experiment_metadata: dict[str, Any] = Field(default_factory=dict)


_CHANNEL_01_BRAND_V1: dict[str, Any] = {
    "brand_key": "channel_01",
    "version": 1,
    "persona": {"key": "youth_host", "version": "2.0.0"},
    "voice_policy": {
        "direction_key": "grounded_teen_v1",
        "preferred_profiles": ["openai_youth_v2", "gemini_youth_v2"],
    },
    "visual": {
        "brand_key": "channel_01",
        "version": 1,
        "theme_key": "signal_v1",
        "palette": {
            "ink": "#101216",
            "paper": "#F6F3EC",
            "signal_blue": "#5B6CFF",
            "hot_peach": "#FF7657",
            "volt": "#D9FF57",
        },
        "captions": {
            "treatment_key": "impact_clean_v1",
            "font_family": "Arial, Helvetica, sans-serif",
            "font_size_px": 66,
            "font_weight": 900,
            "max_visual_lines": 2,
            "bottom_safe_zone_px": 250,
        },
        "motion": {
            "treatment_key": "restrained_punch_v1",
            "max_punch_scale": 1.08,
            "freeze_frame_max_frames": 8,
            "random_motion_enabled": False,
        },
        "end_card": {
            "treatment_key": "verdict_v1",
            "accent_role": "signal_blue",
            "max_question_lines": 3,
        },
    },
    "packaging": {
        "title_family": "specific_curiosity_v1",
        "thumbnail_family": "single_focus_v1",
    },
    "interaction": {
        "allowed_rituals": [
            "official_ruling",
            "pick_a_side",
            "prediction",
            "comment_callback",
            "scoreboard",
        ]
    },
    "experiment_metadata": {"cohort": "launch_baseline"},
}


def channel_01_brand_v1() -> ChannelBrandContract:
    return ChannelBrandContract.model_validate(deepcopy(_CHANNEL_01_BRAND_V1))


def default_brand_contract() -> ChannelBrandContract:
    return channel_01_brand_v1()


def validate_brand_contract(payload: dict[str, Any]) -> ChannelBrandContract:
    return ChannelBrandContract.model_validate(payload)
