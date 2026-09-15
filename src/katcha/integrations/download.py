from __future__ import annotations

import hashlib
import json
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from yt_dlp import YoutubeDL

from katcha.config import Settings, get_settings


@dataclass(slots=True)
class DownloadedMedia:
    path: Path
    sha256: str
    size_bytes: int
    extension: str | None
    title: str | None
    creator: str | None
    platform: str
    canonical_url: str
    source_metadata: dict[str, object]
    media_metadata: dict[str, object]


def canonicalize_url(url: str) -> str:
    parts = urlsplit(url.strip())
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path, parts.query, ""))


def detect_platform(url: str) -> str:
    host = urlsplit(url).netloc.lower().removeprefix("www.")
    if "tiktok.com" in host:
        return "tiktok"
    if "instagram.com" in host:
        return "instagram"
    if "facebook.com" in host or "fb.watch" in host:
        return "facebook"
    if "reddit.com" in host or "redd.it" in host:
        return "reddit"
    if "youtube.com" in host or "youtu.be" in host:
        return "youtube"
    return "generic"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def ffprobe(path: Path) -> dict[str, object]:
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration,size,format_name:stream=index,codec_type,codec_name,width,height,r_frame_rate",
            "-of",
            "json",
            str(path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)


def download(url: str, settings: Settings | None = None) -> DownloadedMedia:
    settings = settings or get_settings()
    canonical = canonicalize_url(url)
    platform = detect_platform(canonical)
    job_dir = settings.work_dir / f"ingest-{uuid.uuid4().hex}"
    job_dir.mkdir(parents=True, exist_ok=False)

    options = {
        "outtmpl": str(job_dir / "%(id)s.%(ext)s"),
        "format": "bv*+ba/b",
        "merge_output_format": "mp4",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "retries": 3,
        "fragment_retries": 3,
    }

    with YoutubeDL(options) as ydl:
        info = ydl.extract_info(canonical, download=True)

    candidates = [p for p in job_dir.iterdir() if p.is_file() and not p.name.endswith(".part")]
    if not candidates:
        raise RuntimeError(f"yt-dlp produced no media file for {canonical}")
    media_path = max(candidates, key=lambda p: p.stat().st_size)

    digest = sha256_file(media_path)
    probe = ffprobe(media_path)
    safe_info = {
        "id": info.get("id"),
        "extractor": info.get("extractor"),
        "extractor_key": info.get("extractor_key"),
        "webpage_url": info.get("webpage_url"),
        "view_count": info.get("view_count"),
        "like_count": info.get("like_count"),
        "comment_count": info.get("comment_count"),
        "timestamp": info.get("timestamp"),
        "uploader_id": info.get("uploader_id"),
    }

    return DownloadedMedia(
        path=media_path,
        sha256=digest,
        size_bytes=media_path.stat().st_size,
        extension=media_path.suffix.lstrip(".") or None,
        title=info.get("title"),
        creator=info.get("uploader") or info.get("channel"),
        platform=platform,
        canonical_url=canonical,
        source_metadata={k: v for k, v in safe_info.items() if v is not None},
        media_metadata=probe,
    )
