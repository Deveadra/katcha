"""Grounding and coverage checks use synthetic frames, never live provider claims."""

import hashlib
import io
from types import SimpleNamespace

import pytest
from PIL import Image

from katcha.services import production_meme_plan as planner


def policy():
    return SimpleNamespace(
        max_assets=6,
        min_assets=4,
        min_media_kinds=2,
        min_visual_assets=2,
        min_coverage_ratio=0.10,
        max_asset_seconds=5,
    )


def plan():
    return planner.MemePlan(
        beats=[
            planner.MemeBeat(
                frame_index=i,
                visible_evidence="A visible synthetic scene",
                kind="audio" if i == 1 else "image",
                search_query="specific reaction meme",
                caption="A playful caption",
                duration_seconds=4,
            )
            for i in range(4)
        ]
    )


def test_plan_requires_early_hook_and_nonoverlapping_actual_coverage():
    times = [4, 25, 46, 66, 87, 108, 128, 148]
    planner.validate_plan(plan(), times, 158, policy())
    missing = plan()
    missing.beats[0].frame_index = 5
    with pytest.raises(ValueError, match="early"):
        planner.validate_plan(missing, times, 158, policy())
    with pytest.raises(ValueError, match="cover each other"):
        planner.validate_plan(plan(), [4, 5, 6, 7, 87, 108, 128, 148], 158, policy())
    short = plan()
    short.beats[0].duration_seconds = 0.5
    with pytest.raises(ValueError, match="too little"):
        planner.validate_plan(short, times, 158, policy())


def test_frames_verify_original_hash_before_decoding(monkeypatch):
    store = SimpleNamespace(
        stat=lambda key: {"size_bytes": 4},
        download_file=lambda key, path: path.write_bytes(b"fake"),
    )
    clip = SimpleNamespace(storage_key="source", sha256="0" * 64)
    monkeypatch.setattr(
        planner.subprocess, "run", lambda *a, **kw: pytest.fail("decoded wrong source")
    )
    with pytest.raises(ValueError, match="changed"):
        planner.source_frames(store, clip, 158)


def test_frame_sheet_uses_recorded_timestamps_and_local_only_decoder(monkeypatch):
    raw = io.BytesIO()
    Image.new("RGB", (384, 216), "red").save(raw, format="JPEG")
    calls = []

    def decode(args, **kwargs):
        calls.append(args)
        assert args[args.index("-protocol_whitelist") + 1] == "file,pipe"
        assert kwargs["timeout"] == 30
        return SimpleNamespace(stdout=raw.getvalue())

    monkeypatch.setattr(planner.subprocess, "run", decode)
    store = SimpleNamespace(
        stat=lambda key: {"size_bytes": 4},
        download_file=lambda key, path: path.write_bytes(b"fake"),
    )
    clip = SimpleNamespace(storage_key="source", sha256=hashlib.sha256(b"fake").hexdigest())
    data, times = planner.source_frames(store, clip, 158)
    assert Image.open(io.BytesIO(data)).size == (1536, 480)
    assert len(calls) == len(times) == 8
    assert [float(args[args.index("-ss") + 1]) for args in calls] == times
