#!/usr/bin/env python3
"""Exercise the ranked-episode renderer through MinIO and the renderer HTTP service."""

from __future__ import annotations

import io
import subprocess
import tempfile
import wave
from pathlib import Path

from katcha.integrations.storage import ObjectStore
from katcha.rendering.client import render_ranked_episode
from katcha.rendering.manifest import channel_01_brand_v1
from katcha.rendering.ranked_episode_manifest import build_ranked_episode_manifest


def _wav_bytes(duration_seconds: float = 0.35, sample_rate: int = 16000) -> bytes:
    frames = int(duration_seconds * sample_rate)
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        handle.writeframes(b"\x00\x00" * frames)
    return buffer.getvalue()


def main() -> int:
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
                "testsrc2=s=540x960:r=30:d=1.2",
                "-c:v",
                "libx264",
                "-preset",
                "ultrafast",
                "-pix_fmt",
                "yuv420p",
                "-an",
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
        key = f"ci/ranked-render/source-{position}.mp4"
        store.put_bytes(source_bytes, key, "video/mp4")
        ordered_items.append(
            {
                "position": position,
                "role": roles[position],
                "clip_id": f"ci-clip-{position}",
                "storage_key": key,
                "source_duration_seconds": 1.2,
                "width": 540,
                "height": 960,
                "native_audio_policy": "mute",
                "audio_volume": 0.0,
                "narration_duck_volume": 0.0,
            }
        )

    narration_assets: list[dict[str, object]] = []
    beats = [
        (0, "opening", None, None, "RankSnaxx renderer smoke."),
        (1, "reveal", 5, "ci-clip-5", "Number five."),
        (2, "reveal", 1, "ci-clip-1", "Number one."),
    ]
    for sequence, placement, position, clip_id, text in beats:
        key = f"ci/ranked-render/narration-{sequence}.wav"
        store.put_bytes(_wav_bytes(), key, "audio/wav")
        narration_assets.append(
            {
                "sequence": sequence,
                "storage_key": key,
                "placement": placement,
                "position": position,
                "clip_id": clip_id,
                "text": text,
                "duration_seconds": 0.35,
            }
        )

    output_key = "ci/ranked-render/output.mp4"
    manifest = build_ranked_episode_manifest(
        short_episode_id="ci-ranked-render-smoke",
        premise="Five synthetic clips for renderer integration testing",
        format_key="ranksnaxx_countdown",
        format_version="1.0.0",
        ordered_items=ordered_items,
        narration_assets=narration_assets,
        selected_style="observational",
        interaction_prompt=None,
        output_key=output_key,
        brand=channel_01_brand_v1(),
        width=540,
        height=960,
        fps=30,
    )
    result = render_ranked_episode(manifest)
    if not result.metadata.get("verified"):
        raise SystemExit(f"renderer did not verify output: {result.metadata}")
    if not store.exists(output_key):
        raise SystemExit("renderer reported success but output object is missing")
    stat = store.stat(output_key)
    if stat["size_bytes"] <= 0:
        raise SystemExit("renderer output object is empty")

    print(
        "PASS: ranked renderer HTTP/S3 integration "
        f"duration={result.duration_seconds}s size={stat['size_bytes']} bytes"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
