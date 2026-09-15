from katcha.rendering.manifest import build_short_manifest


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
