"""Isolated, bounded re-observation of exact measured shot-start frames."""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

from katcha.editorial.media_intake.scene_schemas import SceneIndexV1
from katcha.editorial.media_intake.scene_worker import MAX_FRAMES, MAX_PIXELS, MAX_RESULT_BYTES


def contact_sheet(path: Path, evidence: SceneIndexV1, shot_ids: list[str]) -> bytes:
    import io

    import av
    from PIL import Image

    if not 1 <= len(shot_ids) <= 8 or len(set(shot_ids)) != len(shot_ids):
        raise ValueError("Select at most eight distinct measured frames")
    shots = {shot.shot_id: shot for shot in evidence.shots}
    if not set(shot_ids) <= shots.keys():
        raise ValueError("Selected frames must exist in the measured index")
    expected = {shots[key].keyframe_tick: key for key in shot_ids}
    images = {}
    with av.open(
        str(path),
        mode="r",
        options={"protocol_whitelist": "file", "format_whitelist": "mov,matroska,webm,avi"},
    ) as container:
        stream = next((stream for stream in container.streams if stream.type == "video"), None)
        if (
            stream is None
            or stream.time_base is None
            or (
                stream.time_base.numerator != evidence.timebase_numerator
                or stream.time_base.denominator != evidence.timebase_denominator
            )
        ):
            raise ValueError("Measured stream timebase changed")
        stream.codec_context.thread_count = 1
        previous = -1
        for number, frame in enumerate(container.decode(stream), 1):
            if number > MAX_FRAMES or not 0 < frame.width * frame.height <= MAX_PIXELS:
                raise ValueError("Semantic frame decoding exceeds measured resource bounds")
            if frame.pts is None or frame.time_base != stream.time_base:
                raise ValueError("Exact measured timestamps are required")
            tick = frame.pts - evidence.source_origin_tick
            if tick <= previous or tick < 0 or tick >= evidence.duration_ticks:
                raise ValueError("Measured source timeline changed")
            previous = tick
            if tick not in expected:
                continue
            key = expected[tick]
            image = frame.to_image().convert("RGB")
            measured = image.resize((64, 36)).tobytes()
            if hashlib.sha256(measured).hexdigest() != shots[key].keyframe_sha256:
                raise ValueError("Measured frame hash changed; refuse model inspection")
            # Model receives visible pixels, never just measured RGB/color descriptors.
            images[key] = image.resize((256, 144))
            if len(images) == len(expected):
                break
    if len(images) != len(shot_ids):
        raise ValueError("One or more measured frames are missing")
    sheet = Image.new("RGB", (256 * min(4, len(shot_ids)), 144 * ((len(shot_ids) + 3) // 4)))
    for number, key in enumerate(shot_ids):
        sheet.paste(images[key], (256 * (number % 4), 144 * (number // 4)))
    output = io.BytesIO()
    sheet.save(output, format="JPEG", quality=85)
    data = output.getvalue()
    if len(data) > MAX_RESULT_BYTES:
        raise ValueError("Contact sheet exceeds the bounded image budget")
    return data


def main() -> None:
    import resource

    resource.setrlimit(resource.RLIMIT_CPU, (60, 60))
    resource.setrlimit(resource.RLIMIT_AS, (1536 * 1024 * 1024, 1536 * 1024 * 1024))
    resource.setrlimit(resource.RLIMIT_FSIZE, (MAX_RESULT_BYTES, MAX_RESULT_BYTES))
    resource.setrlimit(resource.RLIMIT_NOFILE, (32, 32))
    request = json.loads(Path(sys.argv[2]).read_bytes())
    evidence = SceneIndexV1.model_validate(request["evidence"])
    Path(sys.argv[3]).write_bytes(contact_sheet(Path(sys.argv[1]), evidence, request["shot_ids"]))


if __name__ == "__main__":
    main()
