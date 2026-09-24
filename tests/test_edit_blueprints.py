import pytest

from katcha.editing.blueprints import (
    header_explainer_v1,
    persona_commentary_v1,
)
from katcha.rendering.blueprint_manifest import build_blueprint_render_manifest
from katcha.rendering.manifest import channel_01_brand_v1


def test_blueprints_encode_distinct_channel_editing_grammars() -> None:
    persona = persona_commentary_v1()
    explainer = header_explainer_v1()

    assert persona.source_layout.mode == "full_frame"
    assert persona.narration.mode == "persona_voice"
    assert persona.narration.required is True
    assert persona.source_layout.background_mode == "blurred_fill"

    assert explainer.source_layout.mode == "header_panel"
    assert explainer.header.required is True
    assert explainer.narration.mode == "text_only"
    assert explainer.narration.source_audio_policy == "retain"


def test_persona_commentary_compiles_deterministically() -> None:
    kwargs = dict(
        render_id="render-1",
        channel_profile_id="channel-a",
        brand=channel_01_brand_v1(),
        blueprint=persona_commentary_v1(),
        source_storage_key="clips/source.mp4",
        source_duration_seconds=18.0,
        narration_asset_key="audio/host.wav",
        narration_text="Watch the second attempt. That is where this stops making sense.",
        narration_duration_seconds=4.0,
        output_key="renders/render-1.mp4",
    )

    first = build_blueprint_render_manifest(**kwargs)
    second = build_blueprint_render_manifest(**kwargs)

    assert first.model_dump(mode="json") == second.model_dump(mode="json")
    assert first.header.enabled is False
    assert first.source.top_px == 0
    assert first.source.background_mode == "blurred_fill"
    assert first.source.audio_volume == pytest.approx(0.14)
    assert first.narration is not None
    assert first.narration.cues


def test_header_explainer_compiles_black_header_without_generated_voice() -> None:
    manifest = build_blueprint_render_manifest(
        render_id="render-2",
        channel_profile_id="channel-b",
        brand=channel_01_brand_v1(),
        blueprint=header_explainer_v1(),
        source_storage_key="clips/movie.mp4",
        source_duration_seconds=24.0,
        headline="The detail that changes what this scene means",
        output_key="renders/render-2.mp4",
    )

    assert manifest.header.enabled is True
    assert manifest.header.height_px == 360
    assert manifest.header.background == "#000000"
    assert manifest.source.top_px == 360
    assert manifest.source.audio_volume == pytest.approx(0.72)
    assert manifest.narration is None


def test_header_explainer_fails_closed_without_explanation() -> None:
    with pytest.raises(ValueError, match="requires explanatory header"):
        build_blueprint_render_manifest(
            render_id="render-3",
            channel_profile_id="channel-b",
            brand=channel_01_brand_v1(),
            blueprint=header_explainer_v1(),
            source_storage_key="clips/movie.mp4",
            source_duration_seconds=24.0,
            output_key="renders/render-3.mp4",
        )


def test_text_only_blueprint_rejects_voice_assets() -> None:
    with pytest.raises(ValueError, match="does not permit generated narration"):
        build_blueprint_render_manifest(
            render_id="render-4",
            channel_profile_id="channel-b",
            brand=channel_01_brand_v1(),
            blueprint=header_explainer_v1(),
            source_storage_key="clips/movie.mp4",
            source_duration_seconds=24.0,
            headline="A concise explanation",
            narration_asset_key="audio/should-not-exist.wav",
            narration_text="This must not be accepted.",
            narration_duration_seconds=1.0,
            output_key="renders/render-4.mp4",
        )


def test_frozen_blueprint_lineage_rejects_identity_tampering() -> None:
    manifest = build_blueprint_render_manifest(
        render_id="render-5",
        channel_profile_id="channel-a",
        brand=channel_01_brand_v1(),
        blueprint=persona_commentary_v1(),
        source_storage_key="clips/source.mp4",
        source_duration_seconds=18.0,
        narration_asset_key="audio/host.wav",
        narration_text="A compact host line.",
        narration_duration_seconds=1.5,
        output_key="renders/render-5.mp4",
    )
    payload = manifest.model_dump(mode="json")
    payload["blueprint_key"] = "header_explainer"

    with pytest.raises(ValueError, match="blueprint snapshot identity"):
        type(manifest).model_validate(payload)
