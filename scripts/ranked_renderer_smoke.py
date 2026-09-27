#!/usr/bin/env python3
"""Exercise the ranked-episode renderer through MinIO and the renderer HTTP service."""

from __future__ import annotations

import io
import os
import re
import subprocess
import tempfile
import wave
from pathlib import Path

from katcha.integrations.storage import ObjectStore
from katcha.rendering.client import render_ranked_episode
from katcha.rendering.manifest import channel_01_brand_v1
from katcha.rendering.ranked_episode_manifest import build_ranked_episode_manifest


def _wav_bytes(duration_seconds: float = 1.4, sample_rate: int = 22050) -> bytes:
    frames = int(duration_seconds * sample_rate)
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        handle.writeframes(b"\x00\x00" * frames)
    return buffer.getvalue()


def _run_id() -> str:
    raw = os.environ.get("KATCHA_RENDER_SMOKE_RUN_ID", "ci-ranked-render-smoke").strip()
    value = re.sub(r"[^A-Za-z0-9._-]+", "-", raw).strip("-")
    if not value:
        raise SystemExit("KATCHA_RENDER_SMOKE_RUN_ID resolved to an empty identifier")
    return value[:96]


def main() -> int:
    run_id = _run_id()
    expected_renderer = os.environ.get("KATCHA_RENDER_SMOKE_EXPECTED_RENDERER", "").strip()
    require_fresh = os.environ.get("KATCHA_RENDER_SMOKE_REQUIRE_FRESH", "").lower() in {
        "1",
        "true",
        "yes",
    }

    store = ObjectStore()
    store.ensure_bucket()

    with tempfile.TemporaryDirectory(prefix="katcha-ranked-render-smoke-") as temp_dir:
        source = Path(temp_dir) / "source.mp4"
        subprocess.run(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-f",
                "lavfi",
                "-i",
                "testsrc2=s=720x1280:r=30:d=5",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=440:sample_rate=48000:duration=5",
                "-shortest",
                "-c:v",
                "libx264",
                "-preset",
                "ultrafast",
                "-crf",
                "28",
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "aac",
                "-b:a",
                "96k",
                "-movflags",
                "+faststart",
                str(source),
            ],
            check=True,
        )
        source_bytes = source.read_bytes()

    ordered_items: list[dict[str, object]] = []
    roles = {
        5: "opener",
        4: "build",
        3: "build",
        2: "false_peak",
        1: "payoff",
    }
    for position in range(5, 0, -1):
        key = f"ci/ranked-render/{run_id}/source-{position}.mp4"
        store.put_bytes(source_bytes, key, "video/mp4")
        ordered_items.append(
            {
                "position": position,
                "role": roles[position],
                "clip_id": f"{run_id}-clip-{position}",
                "storage_key": key,
                "source_duration_seconds": 5.0,
                "width": 720,
                "height": 1280,
                "native_audio_policy": "duck",
                "audio_volume": 0.35,
                "narration_duck_volume": 0.16,
            }
        )

    narration_assets: list[dict[str, object]] = []
    beats = [
        (0, "opening", None, None, "RankSnaxx renderer smoke starts now."),
        (1, "reveal", 5, f"{run_id}-clip-5", "Number five. Starting the countdown."),
        (2, "reveal", 4, f"{run_id}-clip-4", "Number four. The test keeps moving."),
        (3, "reveal", 3, f"{run_id}-clip-3", "Number three. Halfway through."),
        (4, "reveal", 2, f"{run_id}-clip-2", "Number two. This is the false peak."),
        (5, "reveal", 1, f"{run_id}-clip-1", "Number one. This is the final payoff."),
        (6, "closing", None, None, "The fixture countdown is complete."),
        (7, "interaction", None, None, "Which position would you change?"),
    ]
    for sequence, placement, position, clip_id, text in beats:
        key = f"ci/ranked-render/{run_id}/narration-{sequence}.wav"
        store.put_bytes(_wav_bytes(), key, "audio/wav")
        narration_assets.append(
            {
                "sequence": sequence,
                "storage_key": key,
                "placement": placement,
                "position": position,
                "clip_id": clip_id,
                "text": text,
                "duration_seconds": 1.4,
            }
        )

    output_key = f"ci/ranked-render/{run_id}/output.mp4"
    manifest = build_ranked_episode_manifest(
        short_episode_id=run_id,
        premise="Five synthetic clips for renderer integration testing",
        format_key="ranksnaxx_countdown",
        format_version="1.0.0",
        ordered_items=ordered_items,
        narration_assets=narration_assets,
        selected_style="interactive",
        interaction_prompt="Which position would you change?",
        output_key=output_key,
        brand=channel_01_brand_v1(),
        width=1080,
        height=1920,
        fps=30,
    )
    result = render_ranked_episode(manifest)
    if not result.metadata.get("verified"):
        raise SystemExit(f"renderer did not verify output: {result.metadata}")
    if expected_renderer and result.metadata.get("renderer") != expected_renderer:
        raise SystemExit(
            f"expected renderer {expected_renderer!r}, got {result.metadata.get('renderer')!r}"
        )
    if require_fresh and result.metadata.get("reused"):
        raise SystemExit(f"renderer reused an existing output: {result.metadata}")
    if expected_renderer == "remotion-lambda":
        if not result.metadata.get("renderId"):
            raise SystemExit(f"Lambda render did not return renderId: {result.metadata}")
        if int(result.metadata.get("lambdasInvoked") or 0) <= 0:
            raise SystemExit(
                f"Lambda render reported no invoked workers: {result.metadata}"
            )
    if not store.exists(output_key):
        raise SystemExit("renderer reported success but output object is missing")
    stat = store.stat(output_key)
    if stat["size_bytes"] <= 0:
        raise SystemExit("renderer output object is empty")

    print(
        "PASS: ranked renderer HTTP/S3 integration "
        f"run_id={run_id} renderer={result.metadata.get('renderer')} "
        f"duration={result.duration_seconds}s size={stat['size_bytes']} bytes"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
