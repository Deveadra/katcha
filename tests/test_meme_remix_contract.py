"""A downloaded asset alone must not certify a useful remix."""
import pytest

from katcha.editing.blueprints import get_edit_blueprint, meme_v1, meme_v2
from katcha.rendering.blueprint_manifest import (
    BlueprintMemeEvent,
    build_blueprint_render_manifest,
    validate_meme_additions,
)
from katcha.rendering.manifest import channel_01_brand_v1


def event(index, kind="image", start=None, duration=5):
    return BlueprintMemeEvent(
        id=f"beat-{index}", kind=kind,
        storage_key=f"productions/remix/meme/{index}.media",
        source_url="https://example.com/reaction", title="Fixture reaction",
        caption="Fixture contextual reaction", start_seconds=index * 30 if start is None else start,
        duration_seconds=duration,
    )


def manifest(blueprint, events):
    return build_blueprint_render_manifest(
        render_id="remix", channel_profile_id="channel", brand=channel_01_brand_v1(),
        blueprint=blueprint, source_storage_key="source.mp4", source_duration_seconds=158,
        output_key="new-remix.mp4", meme_events=events,
    )


def test_small_legacy_accent_is_readable_but_cannot_certify_new_remix():
    single = [event(0, start=39.545, duration=3)]
    assert manifest(meme_v1(), single).meme_events == single
    with pytest.raises(ValueError, match="too few prepared"):
        manifest(meme_v2(), single)


def test_full_remix_preserves_original_audio_and_caption_in_manifest():
    events = [event(i, "audio" if i == 3 else "image") for i in range(4)]
    result = manifest(meme_v2(), events)
    assert result.source.audio_volume == 1
    assert result.narration is None
    assert result.meme_events[0].caption == events[0].caption
    assert get_edit_blueprint("meme", "2.0.0") == meme_v2()


def test_overlapping_reactions_do_not_inflate_coverage():
    events = [event(i, "audio" if i == 3 else "image", start=4) for i in range(4)]
    with pytest.raises(ValueError, match="cover too little"):
        validate_meme_additions(events, meme_v2().meme, 158)


@pytest.mark.parametrize("kinds,message", [
    (["image"] * 4, "more than one kind"),
    (["audio", "audio", "audio", "image"], "too few visual"),
])
def test_downloads_must_supply_media_variety(kinds, message):
    with pytest.raises(ValueError, match=message):
        manifest(meme_v2(), [event(i, kind) for i, kind in enumerate(kinds)])


def test_actual_truncated_download_durations_must_still_meet_coverage():
    events = [event(i, "audio" if i == 3 else "image", duration=1) for i in range(4)]
    with pytest.raises(ValueError, match="cover too little"):
        manifest(meme_v2(), events)


def test_new_remix_cannot_drop_contextual_captions():
    events = [event(i, "audio" if i == 3 else "image") for i in range(4)]
    events[0].caption = " "
    with pytest.raises(ValueError, match="contextual caption"):
        manifest(meme_v2(), events)
