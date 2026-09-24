from __future__ import annotations

import inspect
from pathlib import Path

from katcha.orchestration import production_activities


def test_text_only_blueprints_skip_tts_before_provider_execution() -> None:
    source = inspect.getsource(production_activities.generate_narration_assets)
    skip_index = source.index('blueprint.narration.mode in {"text_only", "source_only"}')
    synthesize_index = source.index("synthesize_speech(")

    assert skip_index < synthesize_index
    assert "voice_skipped_by_blueprint" in source
    assert "production.voice_skipped" in source


def test_single_clip_text_only_blueprint_routes_to_generic_renderer() -> None:
    build_source = inspect.getsource(
        production_activities.build_render_manifest_activity
    )
    render_source = inspect.getsource(production_activities.render_short_activity)

    assert 'blueprint.narration.mode in {"text_only", "source_only"}' in build_source
    assert "build_blueprint_render_manifest(" in build_source
    assert 'manifest_payload.get("version") == "blueprint-render-v1"' in render_source
    assert "render_fn = render_blueprint" in render_source


def test_render_activity_requires_renderer_verification() -> None:
    source = inspect.getsource(production_activities.render_short_activity)

    assert 'get("verified")' in source
    assert "renderer output failed post-render verification" in source
    assert "production.render_verified" in source


def test_renderer_uses_ffprobe_and_storage_verification() -> None:
    source = Path("renderer/src/index.mjs").read_text(encoding="utf-8")

    assert "ffprobe" in source
    assert "verifyRender(probe, manifest)" in source
    assert "HeadObjectCommand" in source
    assert "verification_mode: 'ffprobe+object-head'" in source
    assert "verified: true" in source
