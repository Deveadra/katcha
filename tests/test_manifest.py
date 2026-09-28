import pytest

from katcha.rendering.manifest import ShortBrandSpec, build_short_manifest, channel_01_brand_v1


def test_manifest_schedules_narration_without_overlap() -> None:
    manifest = build_short_manifest(
        production_id="prod-1",
        source_key="raw/source.mp4",
        source_duration_seconds=10.0,
        source_width=1080,
        source_height=1920,
        source_audio_volume=0.45,
        width=1080,
        height=1920,
        fps=30,
        script_segments=[
            {"placement": "pre", "text": "Watch this carefully."},
            {
                "placement": "mid",
                "text": "This is where it goes wrong.",
                "source_time_seconds": 4.0,
            },
            {"placement": "post", "text": "Would you count that?"},
        ],
        narration_assets=[
            {"segment_index": 0, "storage_key": "a.wav", "duration_seconds": 1.2},
            {"segment_index": 1, "storage_key": "b.wav", "duration_seconds": 1.5},
            {"segment_index": 2, "storage_key": "c.wav", "duration_seconds": 1.1},
        ],
        output_key="production/prod-1/render/short.mp4",
        title_angle="test",
        interaction_prompt="Would you count that?",
    )

    assert len(manifest.overlays) == 3
    assert manifest.overlays[0].start_seconds == 0.15
    assert manifest.overlays[1].start_seconds >= 4.0
    assert manifest.overlays[2].start_seconds >= 10.1
    for left, right in zip(manifest.overlays, manifest.overlays[1:], strict=False):
        assert left.start_seconds + left.duration_seconds <= right.start_seconds
    assert manifest.output_duration_seconds > 11.0
    assert manifest.overlays[-1].cues[-1].end_seconds <= manifest.output_duration_seconds


def test_manifest_uses_versioned_channel_01_visual_defaults() -> None:
    manifest = build_short_manifest(
        production_id="prod-brand",
        source_key="raw/source.mp4",
        source_duration_seconds=5.0,
        source_width=1080,
        source_height=1920,
        source_audio_volume=0.45,
        width=1080,
        height=1920,
        fps=30,
        script_segments=[{"placement": "post", "text": "Official ruling?"}],
        narration_assets=[
            {"segment_index": 0, "storage_key": "a.wav", "duration_seconds": 0.9}
        ],
        output_key="production/prod-brand/render/short.mp4",
        title_angle="verdict",
        interaction_prompt="Fair or foul?",
    )

    assert manifest.brand.brand_key == "channel_01"
    assert manifest.brand.version == 1
    assert manifest.brand.theme_key == "signal_v1"
    assert manifest.brand.captions.treatment_key == "impact_clean_v1"
    assert manifest.brand.motion.treatment_key == "restrained_punch_v1"
    assert manifest.brand.end_card.treatment_key == "verdict_v1"
    assert manifest.brand.palette.signal_blue == "#5B6CFF"
    assert manifest.brand.motion.random_motion_enabled is False



def test_manifest_applies_edit_recipe_media_controls() -> None:
    manifest = build_short_manifest(
        production_id="prod-recipe",
        source_key="raw/source.mp4",
        source_duration_seconds=8.0,
        source_width=1920,
        source_height=1080,
        source_audio_volume=0.7,
        width=1080,
        height=1920,
        fps=30,
        script_segments=[{"placement": "pre", "text": "Recipe controlled narration."}],
        narration_assets=[
            {"segment_index": 0, "storage_key": "voice.wav", "duration_seconds": 1.0}
        ],
        output_key="production/prod-recipe/render/short.mp4",
        title_angle="recipe",
        interaction_prompt=None,
        source_fit="cover",
        source_background_mode="solid",
        source_audio_policy="duck",
        source_duck_volume=0.12,
        captions_enabled=False,
    )

    assert manifest.source.fit == "cover"
    assert manifest.source.background_mode == "solid"
    assert manifest.source.audio_policy == "duck"
    assert manifest.source.audio_volume == 0.7
    assert manifest.source.duck_volume == 0.12
    assert manifest.overlays[0].cues == []


def test_channel_logo_contract_is_positionable_and_fails_closed_without_asset() -> None:
    payload = channel_01_brand_v1().model_dump(mode="json")
    payload["logo"] = {
        "enabled": True,
        "storage_key": "brands/channel/logo.png",
        "x_percent": 91,
        "y_percent": 7,
        "width_percent": 11,
        "opacity": 0.8,
    }

    brand = ShortBrandSpec.model_validate(payload)

    assert brand.logo.enabled is True
    assert brand.logo.x_percent == 91
    assert brand.logo.width_percent == 11

    payload["logo"]["storage_key"] = None
    with pytest.raises(ValueError, match="requires a storage key"):
        ShortBrandSpec.model_validate(payload)
