from __future__ import annotations

import uuid
from typing import Any
from urllib.parse import urlparse

import httpx

from katcha.config import Settings, get_settings
from katcha.integrations.youtube.tokens import get_valid_access_token

REPORTING_ROOT = "https://youtubereporting.googleapis.com/v1"
REACH_REPORT_TYPE = "channel_reach_basic_a1"
ANALYTICS_SCOPE = "https://www.googleapis.com/auth/yt-analytics.readonly"
MONETARY_SCOPE = "https://www.googleapis.com/auth/yt-analytics-monetary.readonly"


class YouTubeReportingError(RuntimeError):
    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


def _headers(
    connection_id: uuid.UUID,
    *,
    settings: Settings | None = None,
) -> dict[str, str]:
    token = get_valid_access_token(connection_id, settings=settings or get_settings())
    return {"Authorization": f"Bearer {token}"}


def _json_request(
    method: str,
    url: str,
    *,
    connection_id: uuid.UUID,
    settings: Settings | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    response = httpx.request(
        method,
        url,
        headers=_headers(connection_id, settings=settings),
        timeout=30,
        **kwargs,
    )
    if response.is_error:
        raise YouTubeReportingError(
            f"YouTube Reporting API request failed ({response.status_code}): "
            f"{response.text[:1200]}",
            status_code=response.status_code,
        )
    return dict(response.json())


def list_reporting_jobs(
    connection_id: uuid.UUID,
    *,
    settings: Settings | None = None,
    max_pages: int = 10,
) -> list[dict[str, Any]]:
    jobs: list[dict[str, Any]] = []
    page_token: str | None = None
    for _ in range(max_pages):
        params: dict[str, str | int] = {"pageSize": 100}
        if page_token:
            params["pageToken"] = page_token
        payload = _json_request(
            "GET",
            f"{REPORTING_ROOT}/jobs",
            connection_id=connection_id,
            settings=settings,
            params=params,
        )
        jobs.extend(dict(item) for item in payload.get("jobs") or [])
        page_token = str(payload.get("nextPageToken") or "") or None
        if page_token is None:
            break
    return jobs


def create_reporting_job(
    connection_id: uuid.UUID,
    *,
    report_type_id: str = REACH_REPORT_TYPE,
    name: str = "katcha-channel-reach",
    settings: Settings | None = None,
) -> dict[str, Any]:
    return _json_request(
        "POST",
        f"{REPORTING_ROOT}/jobs",
        connection_id=connection_id,
        settings=settings,
        json={"reportTypeId": report_type_id, "name": name},
    )


def list_job_reports(
    connection_id: uuid.UUID,
    provider_job_id: str,
    *,
    settings: Settings | None = None,
    max_pages: int = 10,
) -> list[dict[str, Any]]:
    reports: list[dict[str, Any]] = []
    page_token: str | None = None
    for _ in range(max_pages):
        params: dict[str, str | int] = {"pageSize": 100}
        if page_token:
            params["pageToken"] = page_token
        payload = _json_request(
            "GET",
            f"{REPORTING_ROOT}/jobs/{provider_job_id}/reports",
            connection_id=connection_id,
            settings=settings,
            params=params,
        )
        reports.extend(dict(item) for item in payload.get("reports") or [])
        page_token = str(payload.get("nextPageToken") or "") or None
        if page_token is None:
            break
    return reports


def download_report(
    connection_id: uuid.UUID,
    download_url: str,
    *,
    settings: Settings | None = None,
    max_bytes: int = 20 * 1024 * 1024,
) -> bytes:
    parsed = urlparse(download_url)
    if parsed.scheme != "https" or parsed.hostname != "youtubereporting.googleapis.com":
        raise YouTubeReportingError("YouTube report download URL uses an unexpected host")
    response = httpx.get(
        download_url,
        headers=_headers(connection_id, settings=settings),
        timeout=60,
        follow_redirects=False,
    )
    if response.is_redirect:
        raise YouTubeReportingError("YouTube report download unexpectedly redirected")
    if response.is_error:
        raise YouTubeReportingError(
            f"YouTube report download failed ({response.status_code})",
            status_code=response.status_code,
        )
    if len(response.content) > max_bytes:
        raise YouTubeReportingError("YouTube reach report exceeds the configured byte limit")
    return bytes(response.content)
