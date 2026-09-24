from __future__ import annotations

import csv
import io
import uuid
from datetime import datetime
from typing import Any
from urllib.parse import urlparse

import httpx

from katcha.config import Settings, get_settings
from katcha.integrations.youtube.tokens import get_valid_access_token

REPORTING_BASE = "https://youtubereporting.googleapis.com/v1"
REACH_REPORT_TYPE_ID = "channel_reach_basic_a1"
_MAX_PAGES = 20
_MAX_REPORT_BYTES = 50 * 1024 * 1024
_MAX_REPORT_ROWS = 1_000_000
_REQUIRED_REACH_COLUMNS = {
    "date",
    "channel_id",
    "video_id",
    "video_thumbnail_impressions",
    "video_thumbnail_impressions_ctr",
}


class YouTubeReportingError(RuntimeError):
    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class YouTubeReportingClient:
    def __init__(
        self,
        connection_id: uuid.UUID,
        settings: Settings | None = None,
    ) -> None:
        self.connection_id = connection_id
        self.settings = settings or get_settings()

    def _headers(self) -> dict[str, str]:
        token = get_valid_access_token(self.connection_id, settings=self.settings)
        return {"Authorization": f"Bearer {token}"}

    def _json(self, method: str, url: str, **kwargs: Any) -> dict[str, Any]:
        response = httpx.request(
            method,
            url,
            headers={**self._headers(), **dict(kwargs.pop("headers", {}))},
            timeout=30,
            **kwargs,
        )
        if response.is_error:
            raise YouTubeReportingError(
                f"YouTube Reporting request failed ({response.status_code})",
                status_code=response.status_code,
            )
        payload = response.json()
        if not isinstance(payload, dict):
            raise YouTubeReportingError("YouTube Reporting returned an invalid payload")
        return dict(payload)

    def list_jobs(self) -> list[dict[str, Any]]:
        jobs: list[dict[str, Any]] = []
        page_token: str | None = None
        for _ in range(_MAX_PAGES):
            params: dict[str, object] = {"pageSize": 100}
            if page_token:
                params["pageToken"] = page_token
            payload = self._json("GET", f"{REPORTING_BASE}/jobs", params=params)
            jobs.extend(
                dict(item) for item in payload.get("jobs", []) if isinstance(item, dict)
            )
            page_token = str(payload.get("nextPageToken") or "").strip() or None
            if not page_token:
                return jobs
        raise YouTubeReportingError("YouTube Reporting jobs pagination exceeded safety limit")

    def create_job(
        self,
        *,
        report_type_id: str = REACH_REPORT_TYPE_ID,
        name: str = "Katcha thumbnail reach",
    ) -> dict[str, Any]:
        return self._json(
            "POST",
            f"{REPORTING_BASE}/jobs",
            json={"reportTypeId": report_type_id, "name": name},
        )

    def list_reports(
        self,
        job_id: str,
        *,
        created_after: datetime | None = None,
    ) -> list[dict[str, Any]]:
        reports: list[dict[str, Any]] = []
        page_token: str | None = None
        for _ in range(_MAX_PAGES):
            params: dict[str, object] = {"pageSize": 100}
            if created_after is not None:
                params["createdAfter"] = created_after.isoformat().replace("+00:00", "Z")
            if page_token:
                params["pageToken"] = page_token
            payload = self._json(
                "GET",
                f"{REPORTING_BASE}/jobs/{job_id}/reports",
                params=params,
            )
            reports.extend(
                dict(item)
                for item in payload.get("reports", [])
                if isinstance(item, dict)
            )
            page_token = str(payload.get("nextPageToken") or "").strip() or None
            if not page_token:
                return reports
        raise YouTubeReportingError("YouTube Reporting reports pagination exceeded safety limit")

    def download_report(self, download_url: str) -> bytes:
        parsed = urlparse(download_url)
        host = (parsed.hostname or "").casefold()
        if parsed.scheme != "https" or not (
            host == "googleapis.com"
            or host.endswith(".googleapis.com")
            or host == "googleusercontent.com"
            or host.endswith(".googleusercontent.com")
        ):
            raise YouTubeReportingError("YouTube Reporting download URL is not an approved host")

        total = 0
        chunks: list[bytes] = []
        with httpx.stream(
            "GET",
            download_url,
            headers={**self._headers(), "Accept-Encoding": "gzip"},
            timeout=60,
        ) as response:
            if response.is_error:
                raise YouTubeReportingError(
                    f"YouTube Reporting download failed ({response.status_code})",
                    status_code=response.status_code,
                )
            for chunk in response.iter_bytes():
                total += len(chunk)
                if total > _MAX_REPORT_BYTES:
                    raise YouTubeReportingError("YouTube reach report exceeds 50MB safety limit")
                chunks.append(chunk)
        return b"".join(chunks)


def parse_reach_csv(payload: bytes) -> list[dict[str, str]]:
    try:
        text = payload.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise YouTubeReportingError("YouTube reach report is not valid UTF-8") from exc
    reader = csv.DictReader(io.StringIO(text))
    headers = set(reader.fieldnames or [])
    missing = _REQUIRED_REACH_COLUMNS - headers
    if missing:
        raise YouTubeReportingError(
            f"YouTube reach report is missing required columns: {', '.join(sorted(missing))}"
        )
    rows: list[dict[str, str]] = []
    for index, row in enumerate(reader, start=1):
        if index > _MAX_REPORT_ROWS:
            raise YouTubeReportingError("YouTube reach report exceeds row safety limit")
        rows.append({str(key): str(value or "") for key, value in row.items()})
    return rows
