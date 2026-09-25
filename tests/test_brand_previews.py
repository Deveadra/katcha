import hashlib
import inspect
import json
import uuid

import pytest
from pydantic import ValidationError

from katcha.api.brands import CreateBrandPreviewRequest
from katcha.api.main import app, list_productions
from katcha.brand_preview_models import BrandPreviewRender
from katcha.branding import rank_snaxx_brand_v1, rank_snaxx_brand_v2
from katcha.orchestration import brand_preview_activities
from katcha.rendering.manifest import ShortBrandSpec, ShortRenderManifest
from katcha.rendering.ranked_episode_manifest import build_ranked_episode_manifest
from katcha.services.brand_previews import (
    _request_key,
    compile_brand_preview_manifest,
    compile_ranked_brand_preview_manifest,
)


def _base_manifest() -> ShortRenderManifest:
    brand = rank_snaxx_brand_v1()
    return ShortRenderManifest.model_validate(
        {
            "production_id": "source-production",
            "width": 1080,
            "height": 1920,
            "fps": 30,
            "source": {
                "storage_key": "production/source/source.mp4",
                "duration_seconds": 4.0,
                "width": 1080,
                "height": 1920,
                "audio_volume": 0.35,
            },
            "overlays": [
                {
                    "asset_key": "production/source/audio.wav",
                    "placement": "mid",
                    "text": "That is absolutely wild.",
                    "start_seconds": 0.5,
                    "duration_seconds": 1.5,
                    "source_time_seconds": 0.5,
                    "cues": [],
                }
            ],
            "output_duration_seconds": 4.2,
            "output_key": "production/source/render/short-g1.mp4",
            "title_angle": "Original angle",
            "interaction_prompt": None,
            "brand": brand.visual,
            "reaction_events": [],
        }
    )


def test_preview_compiler_swaps_only_candidate_brand_and_preview_treatment() -> None:
    base = _base_manifest()
    original = base.model_dump(mode="json")
    candidate = rank_snaxx_brand_v2()
    preview_id = uuid.uuid4()
    channel_id = uuid.uuid4()
    production_id = uuid.uuid4()

    preview, cue = compile_brand_preview_manifest(
        base,
        preview_id=preview_id,
        channel_profile_id=channel_id,
        contract_visual=dict(candidate.visual),
        brand_key=candidate.brand_key,
        brand_version=candidate.version,
        production_id=production_id,
        reaction_cue={
            "id": "preview-meme-cry",
            "asset_key": "meme_cry",
            "line_ref": 0,
            "offset_seconds": 0.2,
            "duration_seconds": 1.0,
            "anchor": "bottom_right",
            "animation": "pop_bounce",
            "scale": 0.22,
        },
    )

    assert base.model_dump(mode="json") == original
    assert base.brand.version == 1
    assert base.reaction_events == []

    assert preview.brand.brand_key == "ranksnaxx"
    assert preview.brand.version == 2
    assert preview.production_id == f"brand-preview-{preview_id}"
    assert preview.output_key.startswith(
        f"previews/brands/{channel_id}/v2/production-{production_id}/"
    )
    assert len(preview.reaction_events) == 1
    assert preview.reaction_events[0].storage_key == (
        "brands/ranksnaxx/reactions/host_emotes/v1/meme_cry.png"
    )
    assert cue is not None
    assert cue["asset_key"] == "meme_cry"


def test_brand_preview_ledger_has_no_publication_lineage() -> None:
    columns = set(BrandPreviewRender.__table__.columns.keys())

    assert "production_id" in columns
    assert "brand_version_id" in columns
    assert "output_key" in columns
    assert "publication_id" not in columns
    assert "youtube_connection_id" not in columns


def test_brand_preview_control_routes_are_mounted() -> None:
    paths = set(app.openapi()["paths"])

    assert (
        "/v1/channels/{channel_profile_id}/brands/{version}/previews"
        in paths
    )
    assert (
        "/v1/channels/{channel_profile_id}/brand-previews/{preview_id}"
        in paths
    )


def test_brand_preview_media_route_is_mounted() -> None:
    paths = set(app.openapi()["paths"])

    assert (
        "/v1/channels/{channel_profile_id}/brand-previews/{preview_id}/media"
        in paths
    )


def test_production_listing_accepts_channel_scope() -> None:
    parameters = inspect.signature(list_productions).parameters

    assert "channel_profile_id" in parameters



def _ranked_manifest():
    brand = rank_snaxx_brand_v1()
    return build_ranked_episode_manifest(
        short_episode_id="source-episode",
        premise="Three escalating moments",
        format_key="ranksnaxx_countdown",
        format_version="1.0.0",
        ordered_items=[
            {
                "position": position,
                "role": role,
                "clip_id": f"clip-{position}",
                "storage_key": f"raw/clip-{position}.mp4",
                "source_duration_seconds": 4.0,
            }
            for position, role in [(3, "opener"), (2, "build"), (1, "payoff")]
        ],
        narration_assets=[
            {
                "sequence": 4,
                "storage_key": "narration/opening.wav",
                "placement": "opening",
                "text": "Watch how quickly this escalates.",
                "duration_seconds": 1.4,
            }
        ],
        selected_style="observational",
        interaction_prompt=None,
        output_key="short-episodes/source-episode/renders/g1.mp4",
        brand=ShortBrandSpec.model_validate(brand.visual),
    )


def test_ranked_preview_preserves_episode_timeline_and_swaps_brand() -> None:
    base = _ranked_manifest()
    original = base.model_dump(mode="json")
    candidate = rank_snaxx_brand_v2()
    preview_id = uuid.uuid4()
    channel_id = uuid.uuid4()
    episode_id = uuid.uuid4()

    preview, cue = compile_ranked_brand_preview_manifest(
        base,
        preview_id=preview_id,
        channel_profile_id=channel_id,
        contract_visual=dict(candidate.visual),
        brand_key=candidate.brand_key,
        brand_version=candidate.version,
        short_episode_id=episode_id,
        reaction_cue={
            "id": "preview-ranked-meme-cry",
            "asset_key": "meme_cry",
            "line_ref": 4,
            "offset_seconds": 0.2,
            "duration_seconds": 1.0,
            "anchor": "bottom_right",
            "animation": "pop_bounce",
            "scale": 0.22,
        },
    )

    assert base.model_dump(mode="json") == original
    assert preview.items == base.items
    assert preview.overlays == base.overlays
    assert preview.end_card == base.end_card
    assert preview.brand.version == 2
    assert preview.treatment.brand_key == "ranksnaxx"
    assert preview.treatment.brand_version == 2
    assert preview.output_key.startswith(
        f"previews/brands/{channel_id}/v2/short-episode-{episode_id}/"
    )
    assert preview.reaction_events[0].line_ref == 4
    assert preview.reaction_events[0].storage_key.endswith("/v1/meme_cry.png")
    assert cue is not None and cue["line_ref"] == 4


def test_brand_preview_ledger_requires_exactly_one_source() -> None:
    columns = set(BrandPreviewRender.__table__.columns.keys())
    constraints = {
        constraint.name
        for constraint in BrandPreviewRender.__table__.constraints
        if constraint.name
    }

    assert {"production_id", "short_episode_id"} <= columns
    assert "ck_brand_preview_exactly_one_source" in constraints


def test_preview_request_requires_exactly_one_source() -> None:
    production_id = uuid.uuid4()
    episode_id = uuid.uuid4()

    assert CreateBrandPreviewRequest(production_id=production_id).production_id == production_id
    assert (
        CreateBrandPreviewRequest(short_episode_id=episode_id).short_episode_id
        == episode_id
    )
    with pytest.raises(ValidationError, match="exactly one preview source"):
        CreateBrandPreviewRequest()
    with pytest.raises(ValidationError, match="exactly one preview source"):
        CreateBrandPreviewRequest(
            production_id=production_id,
            short_episode_id=episode_id,
        )



def test_production_preview_request_key_remains_backward_compatible() -> None:
    source_id = uuid.uuid4()
    cue = {"asset_key": "meme_cry", "line_ref": 0}
    legacy_payload = {
        "production_id": str(source_id),
        "brand_version": 2,
        "reaction_cue": cue,
    }
    expected = hashlib.sha256(
        json.dumps(
            legacy_payload,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()

    assert _request_key("production", source_id, 2, cue) == expected
    assert _request_key("short_episode", source_id, 2, cue) != expected


def test_preview_activity_dispatches_both_render_manifest_families() -> None:
    source = inspect.getsource(brand_preview_activities.render_brand_preview_activity)

    assert '"short-render-v1"' in source
    assert '"ranked-episode-render-v1"' in source
    assert "render_fn = render_short" in source
    assert "render_fn = render_ranked_episode" in source
