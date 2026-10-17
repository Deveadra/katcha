from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from yt_dlp import YoutubeDL

from katcha.config import Settings, get_settings
from katcha.orchestration.failure_details import (
    YOUTUBE_DOWNLOAD_AUTH_RECOVERY,
    MediaPreparationError,
)


class MediaDownloadError(MediaPreparationError):
    pass


class DownloadAuthenticationRequired(MediaDownloadError):
    diagnostic_code = "youtube_authentication_required"
    diagnostic_retryable = False
    diagnostic_recovery = YOUTUBE_DOWNLOAD_AUTH_RECOVERY


class DownloadSessionConfigurationError(MediaDownloadError):
    diagnostic_code = "youtube_download_session_invalid"
    diagnostic_retryable = False
    diagnostic_recovery = (
        "The operator-managed YouTube session file could not be loaded. "
        "Check its format and permissions, or supply a replacement authorized session."
    )


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
    host = (urlsplit(url).hostname or "").lower().removeprefix("www.")
    for platform, domains in {
        "tiktok": ("tiktok.com",), "instagram": ("instagram.com",),
        "facebook": ("facebook.com", "fb.watch"), "reddit": ("reddit.com", "redd.it"),
        "youtube": ("youtube.com", "youtu.be"),
    }.items():
        if any(host == domain or host.endswith("." + domain) for domain in domains):
            return platform
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
        timeout=120,
    )
    return json.loads(result.stdout)


def _is_youtube_auth_challenge(exc: Exception) -> bool:
    message = str(exc).casefold()
    return "sign in to confirm" in message or "sign in to view" in message


def download(url: str, settings: Settings | None = None) -> DownloadedMedia:
    settings = settings or get_settings()
    canonical = canonicalize_url(url)
    platform = detect_platform(canonical)
    job_dir = settings.work_dir / f"ingest-{uuid.uuid4().hex}"
    job_dir.mkdir(mode=0o700, parents=True, exist_ok=False)

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

    stage = "download"
    cookie_copy = job_dir / "server-session.cookies"
    session_attempted = False
    try:
        # Public videos commonly work without a login. Never expose the
        # operator-managed session unless anonymous access actually fails
        # with a recognized YouTube authentication challenge.
        try:
            with YoutubeDL(options) as ydl:
                info = ydl.extract_info(canonical, download=True)
        except Exception as first_error:
            if (
                platform != "youtube"
                or settings.youtube_download_cookie_file is None
                or not _is_youtube_auth_challenge(first_error)
            ):
                raise

            # A failed extractor may leave fragments or partial outputs.
            # Retry in a clean private directory, not against those files.
            shutil.rmtree(job_dir)
            job_dir.mkdir(mode=0o700)
            stage = "download authentication configuration"
            try:
                # yt-dlp may rewrite cookies on exit; isolate every job from
                # the read-only operator secret and other concurrent jobs.
                fd = os.open(cookie_copy, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                os.close(fd)
                shutil.copyfile(settings.youtube_download_cookie_file, cookie_copy)
            except OSError:
                raise DownloadSessionConfigurationError(
                    stage,
                    RuntimeError("The configured server session file could not be loaded"),
                ) from None
            options["cookiefile"] = str(cookie_copy)
            session_attempted = True
            stage = "download"
            with YoutubeDL(options) as ydl:
                info = ydl.extract_info(canonical, download=True)

        stage = "download output"
        candidates = [
            p for p in job_dir.iterdir()
            if p.is_file() and p != cookie_copy and not p.name.endswith(".part")
        ]
        if not candidates:
            raise RuntimeError(f"yt-dlp produced no media file for {canonical}")
        media_path = max(candidates, key=lambda p: p.stat().st_size)

        stage = "file integrity"
        digest = sha256_file(media_path)
        stage = "media inspection (ffprobe)"
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
            source_metadata={
                **{k: v for k, v in safe_info.items() if v is not None},
                **({"youtube_access_mode": "session_fallback" if session_attempted else "public"}
                   if platform == "youtube" else {}),
            },
            media_metadata=probe,
        )
    except Exception as exc:
        shutil.rmtree(job_dir, ignore_errors=True)
        if isinstance(exc, DownloadSessionConfigurationError):
            raise
        message = str(exc).casefold()
        if platform == "youtube" and session_attempted and (
            "cookie" in type(exc).__name__.casefold() or "netscape" in message
        ):
            # Cookie parsers can include a complete secret-bearing row in their
            # error and nested cause. Do not retain that chain in receipts/logs.
            raise DownloadSessionConfigurationError(
                "download authentication configuration",
                RuntimeError("The configured server session file could not be loaded"),
            ) from None
        if platform == "youtube" and stage == "download" and _is_youtube_auth_challenge(exc):
            raise DownloadAuthenticationRequired(stage, exc) from exc
        raise MediaDownloadError(stage, exc) from exc
