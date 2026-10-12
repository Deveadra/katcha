"""Local PyAV visual-change measurement, run in a resource-limited child process."""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from typing import Any

from katcha.editorial.media_intake.scene_schemas import MeasuredShotV1, SceneIndexV1

MAX_FRAMES = 36000
MAX_SHOTS = 512
MAX_SECONDS = 600
MAX_PIXELS = 16_777_216
MAX_RESULT_BYTES = 2 * 1024 * 1024
CUT_THRESHOLD = 0.22


def _tags(rgb: tuple[int, int, int]) -> list[str]:
    tags = []
    names = ("red", "green", "blue")
    strongest = max(range(3), key=lambda i: rgb[i])
    others = [rgb[i] for i in range(3) if i != strongest]
    if rgb[strongest] > 50 and rgb[strongest] > max(others) * 1.25:
        tags.append(names[strongest])
    if sum(rgb) / 3 < 45:
        tags.append("dark")
    elif sum(rgb) / 3 > 210:
        tags.append("bright")
    else:
        tags.append("mid-tone")
    return tags


def measure_video(path: Path, clip_sha256: str) -> SceneIndexV1:
    import av

    shots: list[dict[str, Any]] = []
    previous: bytes | None = None
    origin: int | None = None
    previous_tick = -1
    final_end = 0
    count = 0
    with av.open(
        str(path),
        mode="r",
        options={"protocol_whitelist": "file", "format_whitelist": "mov,matroska,webm,avi"},
    ) as container:
        stream = next((stream for stream in container.streams if stream.type == "video"), None)
        if not isinstance(stream, av.video.stream.VideoStream) or stream.time_base is None:
            raise ValueError("A measurable video stream is required")
        timebase = stream.time_base
        if not 0 < stream.width * stream.height <= MAX_PIXELS:
            raise ValueError("Video dimensions exceed the indexing budget")
        stream.codec_context.thread_count = 1
        for frame in container.decode(stream):
            if not isinstance(frame, av.VideoFrame):
                raise ValueError("Expected measured video frames")
            count += 1
            if count > MAX_FRAMES:
                raise ValueError("Decoded frame count exceeds the indexing budget")
            if frame.pts is None or frame.time_base != timebase:
                raise ValueError("Decoded frames require an exact source timebase")
            if not 0 < frame.width * frame.height <= MAX_PIXELS:
                raise ValueError("Decoded dimensions exceed the indexing budget")
            if origin is None:
                origin = frame.pts
            tick = frame.pts - origin
            if tick <= previous_tick:
                raise ValueError("Decoded source timestamps must increase")
            duration = int(frame.duration or 0)
            if duration <= 0:
                raise ValueError("Decoded frame duration is unavailable")
            final_end = tick + duration
            if final_end * timebase > MAX_SECONDS:
                raise ValueError("Video duration exceeds the indexing budget")
            image = frame.to_image().convert("RGB").resize((64, 36))  # type: ignore[no-untyped-call]
            pixels = image.tobytes()
            change = (
                sum(abs(left - right) for left, right in zip(previous, pixels, strict=True))
                / (len(pixels) * 255)
                if previous is not None
                else 1.0
            )
            if previous is None or change >= CUT_THRESHOLD:
                if len(shots) >= MAX_SHOTS:
                    raise ValueError("Shot count exceeds the indexing budget")
                if shots:
                    shots[-1]["end_tick"] = tick
                rgb = tuple(sum(pixels[i::3]) // (64 * 36) for i in range(3))
                shots.append(
                    {
                        "shot_id": f"shot-{len(shots):04d}",
                        "start_tick": tick,
                        "end_tick": final_end,
                        "keyframe_tick": tick,
                        "keyframe_sha256": hashlib.sha256(pixels).hexdigest(),
                        "mean_rgb": rgb,
                        "visual_tags": _tags(rgb),
                    }
                )
            previous = pixels
            previous_tick = tick
        if not shots or origin is None:
            raise ValueError("Video has no decodable frames")
        shots[-1]["end_tick"] = final_end
    return SceneIndexV1(
        clip_sha256=clip_sha256,
        decoder_version=f"PyAV/{av.__version__}",
        timebase_numerator=timebase.numerator,
        timebase_denominator=timebase.denominator,
        source_origin_tick=origin,
        duration_ticks=final_end,
        decoded_frames=count,
        shots=[MeasuredShotV1.model_validate(shot) for shot in shots],
    )


def main() -> None:
    # Limits apply only inside the isolated decoder, never to the API process.
    import resource

    resource.setrlimit(resource.RLIMIT_CPU, (60, 60))
    resource.setrlimit(resource.RLIMIT_AS, (1536 * 1024 * 1024, 1536 * 1024 * 1024))
    resource.setrlimit(resource.RLIMIT_FSIZE, (MAX_RESULT_BYTES, MAX_RESULT_BYTES))
    resource.setrlimit(resource.RLIMIT_NOFILE, (32, 32))
    evidence = measure_video(Path(sys.argv[1]), sys.argv[3])
    encoded = json.dumps(evidence.model_dump(mode="json"), separators=(",", ":")).encode()
    if len(encoded) > MAX_RESULT_BYTES:
        raise ValueError("Index evidence exceeds the supported size")
    Path(sys.argv[2]).write_bytes(encoded)


if __name__ == "__main__":
    main()
