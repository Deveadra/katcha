from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx

from katcha.config import Settings, get_settings
from katcha.integrations.youtube.tokens import get_valid_access_token

DATA_API_BASE = "https://www.googleapis.com/youtube/v3"
UPLOAD_URL = "https://www.googleapis.com/upload/youtube/v3/videos"


class YouTubeAPIError(RuntimeError):
    pass


class UploadSessionExpired(YouTubeAPIError):
    pass


@dataclass(frozen=True, slots=True)
class UploadProgress:
    offset: int
    complete: bool
    response: dict[str, Any] | None = None


def _iso_z(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _raise_api_error(prefix: str, response: httpx.Response) -> None:
    raise YouTubeAPIError(f"{prefix} ({response.status_code}): {response.text[:2000]}")


class YouTubeClient:
    def __init__(
        self,
        connection_id: uuid.UUID,
        settings: Settings | None = None,
    ) -> None:
        self.connection_id = connection_id
        self.settings = settings or get_settings()

    def _token(self) -> str:
        return get_valid_access_token(self.connection_id, settings=self.settings)

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token()}"}

    def initiate_resumable_upload(
        self,
        *,
        title: str,
        description: str,
        tags: list[str],
        category_id: str,
        size_bytes: int,
        mime_type: str,
        notify_subscribers: bool,
        made_for_kids: bool,
        contains_synthetic_media: bool,
    ) -> str:
        body = {
            "snippet": {
                "title": title,
                "description": description,
                "tags": tags,
                "categoryId": category_id,
            },
            "status": {
                "privacyStatus": "private",
                "selfDeclaredMadeForKids": made_for_kids,
                "containsSyntheticMedia": contains_synthetic_media,
                "embeddable": True,
                "publicStatsViewable": True,
            },
        }
        headers = {
            **self._headers(),
            "Content-Type": "application/json; charset=UTF-8",
            "X-Upload-Content-Length": str(size_bytes),
            "X-Upload-Content-Type": mime_type,
        }
        response = httpx.post(
            UPLOAD_URL,
            params={
                "uploadType": "resumable",
                "part": "snippet,status",
                "notifySubscribers": str(notify_subscribers).lower(),
            },
            headers=headers,
            json=body,
            timeout=30,
        )
        if response.is_error:
            _raise_api_error("YouTube upload initiation failed", response)
        location = response.headers.get("Location")
        if not location:
            raise YouTubeAPIError("YouTube resumable upload did not return a Location header")
        return location

    def query_upload(self, upload_url: str, total_size: int) -> UploadProgress:
        response = httpx.put(
            upload_url,
            headers={
                **self._headers(),
                "Content-Length": "0",
                "Content-Range": f"bytes */{total_size}",
            },
            content=b"",
            timeout=30,
        )
        if response.status_code in {200, 201}:
            return UploadProgress(total_size, True, response.json())
        if response.status_code == 308:
            return UploadProgress(_next_offset(response.headers.get("Range")), False)
        if response.status_code in {404, 410}:
            raise UploadSessionExpired("YouTube resumable upload session expired")
        _raise_api_error("YouTube upload status query failed", response)

    def upload_chunk(
        self,
        upload_url: str,
        *,
        data: bytes,
        start: int,
        total_size: int,
        mime_type: str,
    ) -> UploadProgress:
        if not data:
            raise ValueError("upload chunk cannot be empty")
        end = start + len(data) - 1
        response = httpx.put(
            upload_url,
            headers={
                **self._headers(),
                "Content-Type": mime_type,
                "Content-Length": str(len(data)),
                "Content-Range": f"bytes {start}-{end}/{total_size}",
            },
            content=data,
            timeout=120,
        )
        if response.status_code in {200, 201}:
            return UploadProgress(total_size, True, response.json())
        if response.status_code == 308:
            return UploadProgress(_next_offset(response.headers.get("Range")), False)
        if response.status_code in {404, 410}:
            raise UploadSessionExpired("YouTube resumable upload session expired")
        _raise_api_error("YouTube chunk upload failed", response)

    def video_resource(self, video_id: str) -> dict[str, Any]:
        response = httpx.get(
            f"{DATA_API_BASE}/videos",
            params={
                "part": "status,processingDetails,statistics,snippet,contentDetails",
                "id": video_id,
            },
            headers=self._headers(),
            timeout=30,
        )
        if response.is_error:
            _raise_api_error("YouTube video lookup failed", response)
        items = response.json().get("items") or []
        if not items:
            raise YouTubeAPIError(f"YouTube video not found: {video_id}")
        return dict(items[0])

    def schedule_video(
        self,
        video_id: str,
        *,
        publish_at: datetime,
        made_for_kids: bool,
        contains_synthetic_media: bool,
    ) -> dict[str, Any]:
        return self._update_status(
            video_id,
            privacy_status="private",
            publish_at=publish_at,
            made_for_kids=made_for_kids,
            contains_synthetic_media=contains_synthetic_media,
        )

    def set_privacy(
        self,
        video_id: str,
        *,
        privacy_status: str,
        made_for_kids: bool,
        contains_synthetic_media: bool,
    ) -> dict[str, Any]:
        if privacy_status not in {"private", "unlisted", "public"}:
            raise ValueError(f"unsupported YouTube privacy status: {privacy_status}")
        return self._update_status(
            video_id,
            privacy_status=privacy_status,
            publish_at=None,
            made_for_kids=made_for_kids,
            contains_synthetic_media=contains_synthetic_media,
        )

    def _update_status(
        self,
        video_id: str,
        *,
        privacy_status: str,
        publish_at: datetime | None,
        made_for_kids: bool,
        contains_synthetic_media: bool,
    ) -> dict[str, Any]:
        video_status: dict[str, Any] = {
            "privacyStatus": privacy_status,
            "selfDeclaredMadeForKids": made_for_kids,
            "containsSyntheticMedia": contains_synthetic_media,
            "embeddable": True,
            "publicStatsViewable": True,
            "license": "youtube",
        }
        if publish_at is not None:
            video_status["publishAt"] = _iso_z(publish_at)
        response = httpx.put(
            f"{DATA_API_BASE}/videos",
            params={"part": "status"},
            headers={**self._headers(), "Content-Type": "application/json"},
            json={"id": video_id, "status": video_status},
            timeout=30,
        )
        if response.is_error:
            _raise_api_error("YouTube status update failed", response)
        return dict(response.json())


def _next_offset(range_header: str | None) -> int:
    if not range_header:
        return 0
    try:
        _unit, raw = range_header.split("=", 1)
        _start, end = raw.split("-", 1)
        return int(end) + 1
    except (TypeError, ValueError) as exc:
        raise YouTubeAPIError(f"invalid resumable Range header: {range_header}") from exc


def read_chunk(path: Path, offset: int, chunk_size: int) -> bytes:
    with path.open("rb") as handle:
        handle.seek(offset)
        return handle.read(chunk_size)
