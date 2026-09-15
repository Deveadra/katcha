from __future__ import annotations

import math
import subprocess
from dataclasses import dataclass
from pathlib import Path

import imagehash
from PIL import Image, ImageOps


@dataclass(frozen=True, slots=True)
class PreprocessResult:
    frame_paths: list[Path]
    contact_sheet_path: Path
    perceptual_hashes: list[str]
    mean_hash_distance: float


def sample_timestamps(duration_seconds: float, frame_count: int) -> list[float]:
    if frame_count < 1:
        raise ValueError("frame_count must be positive")
    if duration_seconds <= 0:
        return [0.0] * frame_count
    if frame_count == 1:
        return [duration_seconds / 2]
    inset = min(duration_seconds * 0.03, 0.25)
    start = inset
    end = max(start, duration_seconds - inset)
    step = (end - start) / (frame_count - 1)
    return [round(start + (step * index), 3) for index in range(frame_count)]


def _extract_frame(input_path: Path, timestamp: float, output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-ss",
            f"{timestamp:.3f}",
            "-i",
            str(input_path),
            "-frames:v",
            "1",
            "-vf",
            "scale=480:-2",
            "-q:v",
            "3",
            "-y",
            str(output_path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )


def _contact_sheet(frame_paths: list[Path], output_path: Path, columns: int = 3) -> None:
    images = [ImageOps.exif_transpose(Image.open(path)).convert("RGB") for path in frame_paths]
    try:
        tile_width = max(image.width for image in images)
        tile_height = max(image.height for image in images)
        rows = math.ceil(len(images) / columns)
        sheet = Image.new("RGB", (tile_width * columns, tile_height * rows), "black")
        for index, image in enumerate(images):
            x = (index % columns) * tile_width
            y = (index // columns) * tile_height
            fitted = ImageOps.contain(image, (tile_width, tile_height))
            offset_x = x + ((tile_width - fitted.width) // 2)
            offset_y = y + ((tile_height - fitted.height) // 2)
            sheet.paste(fitted, (offset_x, offset_y))
        output_path.parent.mkdir(parents=True, exist_ok=True)
        sheet.save(output_path, format="JPEG", quality=88, optimize=True)
    finally:
        for image in images:
            image.close()


def _hash_distance(hashes: list[imagehash.ImageHash]) -> float:
    if len(hashes) < 2:
        return 0.0
    distances = [
        float(left - right) for left, right in zip(hashes[:-1], hashes[1:], strict=True)
    ]
    return sum(distances) / len(distances)


def preprocess_video(
    input_path: Path,
    output_dir: Path,
    *,
    duration_seconds: float,
    frame_count: int = 6,
) -> PreprocessResult:
    output_dir.mkdir(parents=True, exist_ok=True)
    timestamps = sample_timestamps(duration_seconds, frame_count)
    frame_paths: list[Path] = []
    hashes: list[imagehash.ImageHash] = []

    for index, timestamp in enumerate(timestamps):
        frame_path = output_dir / f"frame-{index:02d}.jpg"
        _extract_frame(input_path, timestamp, frame_path)
        frame_paths.append(frame_path)
        with Image.open(frame_path) as image:
            hashes.append(imagehash.phash(image.convert("RGB")))

    contact_sheet_path = output_dir / "contact-sheet.jpg"
    _contact_sheet(frame_paths, contact_sheet_path)
    return PreprocessResult(
        frame_paths=frame_paths,
        contact_sheet_path=contact_sheet_path,
        perceptual_hashes=[str(value) for value in hashes],
        mean_hash_distance=round(_hash_distance(hashes), 3),
    )
