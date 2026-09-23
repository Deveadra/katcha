import pytest
from pydantic import ValidationError

from katcha.rendering.manifest import ShortBrandSpec, ShortRenderManifest, build_short_manifest
from katcha.rendering.reactions import ReactionAssetPack, ReactionCue, ReactionEvent
from katcha.rendering.ranked_episode_manifest import build_ranked_episode_manifest


def _pack(brand: str = "channel_01") -> ReactionAssetPack:
    return ReactionAssetPack(
        brand_key=brand,
        pack_key="host_emotes",
        version=1,
        assets={
            "meme_cry": {
                "storage_key": (
                    f"brands/{brand}/reactions/host_emotes/v1/meme_cry.png"
                )
            }
        },
    )


def _cue(**overrides: object) -> dict[str, object]:
    return {
        "id": "reaction-1",
        "asset_key": "meme_cry",
        "line_ref": 0,
        "offset_seconds": 0.2,
        "duration_seconds": 1.0,
        **overrides,
    }


def _short(
    *,
    pack: ReactionAssetPack | None = None,
    cues: list[dict[str, object]] | None = None,
    brand: ShortBrandSpec | None = None,
) -> ShortRenderManifest:
    return build_short_manifest(
        production_id="prod-react",
        source_key="raw/source.mp4",
        source_duration_seconds=5,
        source_width=1080,
        source_height=1920,
        source_audio_volume=0.4,
        width=1080,
        height=1920,
        fps=30,
        script_segments=[{"placement": "pre", "text": "All that ice cream wasted!"}],
        narration_assets=[
            {"segment_index": 0, "storage_key": "vo.wav", "duration_seconds": 1.5}
        ],
        output_key="production/prod-react/render/short.mp4",
        title_angle=None,
        interaction_prompt=None,
        brand=brand,
        reaction_pack=pack,
        reaction_cues=cues,
    )


def test_no_reactions_preserves_legacy_render_manifest() -> None:
    original = _short().model_dump(mode="json")
    original.pop("reaction_events")
    restored = ShortRenderManifest.model_validate(original)
    assert restored.reaction_events == []


def test_short_freezes_asset_and_uses_scheduled_voiceover_time() -> None:
    manifest = _short(pack=_pack(), cues=[_cue()])
    event = manifest.reaction_events[0]
    assert event.start_seconds == pytest.approx(0.35)
    assert event.storage_key == (
        "brands/channel_01/reactions/host_emotes/v1/meme_cry.png"
    )
    assert event.pack_version == 1
    assert event.brand_key == manifest.brand.brand_key
    assert ShortRenderManifest.model_validate(
        manifest.model_dump(mode="json")
    ).reaction_events == manifest.reaction_events


@pytest.mark.parametrize(
    ("pack", "cues", "expected"),
    [
        (None, [_cue()], "require an asset pack"),
        (_pack("other_channel"), [_cue()], "different channel"),
        (_pack(), [_cue(asset_key="missing")], "unknown reaction asset"),
        (_pack(), [_cue(line_ref=10)], "unknown narration line_ref"),
        (_pack(), [_cue(offset_seconds=2)], "after narration line"),
        (_pack(), [_cue(duration_seconds=5)], "beyond render"),
        (_pack(), [_cue(), _cue()], "duplicate reaction cue id"),
    ],
)
def test_reaction_cues_fail_closed(pack, cues, expected) -> None:
    with pytest.raises(ValueError, match=expected):
        _short(pack=pack, cues=cues)


def test_pack_requires_versioned_channel_scoped_pngs() -> None:
    with pytest.raises(ValueError, match="versioned channel path"):
        ReactionAssetPack(
            brand_key="channel_01",
            pack_key="host_emotes",
            version=1,
            assets={"meme_cry": {"storage_key": "brands/other/reactions/x.png"}},
        )


def test_direct_manifest_edits_cannot_cross_channels_or_overrun() -> None:
    manifest = _short(pack=_pack(), cues=[_cue()]).model_dump(mode="json")
    manifest["reaction_events"][0]["brand_key"] = "other_channel"
    with pytest.raises(ValidationError, match="invalid channel-scoped PNG key"):
        ShortRenderManifest.model_validate(manifest)
    manifest["reaction_events"][0]["brand_key"] = "channel_01"
    manifest["reaction_events"][0]["start_seconds"] = 100
    with pytest.raises(ValidationError, match="beyond render"):
        ShortRenderManifest.model_validate(manifest)


def test_cue_validation_rejects_negative_and_unknown_animations() -> None:
    with pytest.raises(ValidationError):
        ReactionCue.model_validate(_cue(offset_seconds=-1))
    with pytest.raises(ValidationError):
        ReactionCue.model_validate(_cue(animation="particle_explosion"))


def test_ranked_manifest_reuses_same_pack_without_channel_special_case() -> None:
    brand = ShortBrandSpec(brand_key="another_channel")
    pack = _pack("another_channel")
    manifest = build_ranked_episode_manifest(
        short_episode_id="ep-reaction",
        premise="Three surprising moments",
        format_key="countdown",
        format_version="1.0.0",
        ordered_items=[
            {
                "position": position,
                "role": role,
                "clip_id": f"clip-{position}",
                "storage_key": f"clip-{position}.mp4",
                "source_duration_seconds": 5.0,
            }
            for position, role in [(3, "opener"), (2, "build"), (1, "payoff")]
        ],
        narration_assets=[
            {
                "sequence": 4,
                "storage_key": "voice.wav",
                "placement": "opening",
                "text": "What a mess.",
                "duration_seconds": 1.4,
            }
        ],
        selected_style="observational",
        interaction_prompt=None,
        output_key="ep-reaction.mp4",
        brand=brand,
        reaction_pack=pack,
        reaction_cues=[_cue(line_ref=4)],
    )
    assert manifest.reaction_events[0].brand_key == "another_channel"
    assert manifest.reaction_events[0].start_seconds == pytest.approx(0.28)
    assert manifest.reaction_events[0].storage_key.startswith(
        "brands/another_channel/reactions/"
    )


def test_direct_frozen_event_key_must_match_pack_lineage() -> None:
    event = _short(pack=_pack(), cues=[_cue()]).reaction_events[0]
    payload = event.model_dump(mode="json")
    payload["storage_key"] = "brands/channel_01/reactions/host_emotes/v2/meme_cry.png"
    with pytest.raises(ValidationError, match="invalid channel-scoped PNG key"):
        ReactionEvent.model_validate(payload)
