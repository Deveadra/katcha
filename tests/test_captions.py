from katcha.audio.captions import build_caption_cues


def test_caption_cues_cover_full_duration_without_overlap() -> None:
    cues = build_caption_cues(
        "This is a short caption timing test for Katcha",
        duration_seconds=4.8,
        start_offset_seconds=1.2,
        words_per_cue=3,
    )

    assert cues
    assert cues[0].start_seconds == 1.2
    assert cues[-1].end_seconds == 6.0
    for left, right in zip(cues, cues[1:], strict=False):
        assert left.end_seconds == right.start_seconds
        assert left.end_seconds > left.start_seconds
