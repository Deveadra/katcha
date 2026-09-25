import inspect
import uuid

from katcha.api.main import app, list_productions
from katcha.brand_preview_models import BrandPreviewRender
from katcha.branding import rank_snaxx_brand_v1, rank_snaxx_brand_v2
from katcha.rendering.manifest import ShortRenderManifest
from katcha.services.brand_previews import compile_brand_preview_manifest


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
