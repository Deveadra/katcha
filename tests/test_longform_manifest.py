from katcha.rendering.longform_manifest import build_longform_manifest


def test_longform_manifest_uses_canonical_clip_and_caption_timing() -> None:
    manifest = build_longform_manifest(
        compilation_id="comp-1",
        title_angle="Unexpected moments",
        opening_hook="This first one starts badly.",
        intro=None,
        outro=None,
        segments=[
            {
                "clip_id": "clip-1",
                "source_key": "raw/aa/source.mp4",
                "source_duration_seconds": 9.0,
                "source_width": 1080,
                "source_height": 1920,
                "source_start_seconds": 1.0,
                "source_end_seconds": 8.0,
                "transition_before": "It gets worse.",
                "host_before": None,
                "host_after": "Somehow, that counted.",
            }
        ],
        narration_assets={
            "opening": {
                "storage_key": "compilation/c/audio/opening.wav",
                "duration_seconds": 2.0,
            },
            "seg:0:after": {
                "storage_key": "compilation/c/audio/after.wav",
                "duration_seconds": 1.5,
            },
        },
        source_audio_volume=0.45,
        width=1920,
        height=1080,
        fps=30,
        output_key="compilation/c/render/longform.mp4",
    )

    clip_items = [item for item in manifest.timeline if item.kind == "clip"]
    narration_items = [item for item in manifest.timeline if item.kind == "narration"]

    assert len(clip_items) == 1
    assert clip_items[0].clip is not None
    assert clip_items[0].clip.storage_key == "raw/aa/source.mp4"
    assert clip_items[0].clip.source_start_seconds == 1.0
    assert clip_items[0].clip.source_end_seconds == 8.0
    assert len(narration_items) == 2
    assert narration_items[0].narration is not None
    assert narration_items[0].narration.cues
    assert narration_items[0].narration.cues[0].start_seconds == 0
    assert manifest.output_duration_seconds > 10
