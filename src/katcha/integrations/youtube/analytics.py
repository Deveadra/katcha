from __future__ import annotations

import uuid
from datetime import date
from typing import Any

import httpx

from katcha.config import Settings, get_settings
from katcha.integrations.youtube.tokens import get_valid_access_token

ANALYTICS_URL = "https://youtubeanalytics.googleapis.com/v2/reports"

BASIC_METRICS = (
    "views",
    "engagedViews",
    "estimatedMinutesWatched",
    "averageViewDuration",
    "averageViewPercentage",
    "likes",
    "comments",
    "shares",
    "subscribersGained",
    "subscribersLost",
)
BASIC_METRICS_WITHOUT_ENGAGED = tuple(metric for metric in BASIC_METRICS if metric != "engagedViews")
MONETARY_METRICS = (
    "estimatedRevenue",
    "estimatedAdRevenue",
    "monetizedPlaybacks",
)
RETENTION_METRICS = (
    "audienceWatchRatio",
    "relativeRetentionPerformance",
)


class YouTubeAnalyticsError(RuntimeError):
    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


def _query(
    connection_id: uuid.UUID,
    *,
    start_date: date,
    end_date: date,
    metrics: tuple[str, ...],
    video_id: str,
    dimensions: str | None = None,
    settings: Settings | None = None,
) -> dict[str, Any]:
    settings = settings or get_settings()
    access_token = get_valid_access_token(connection_id, settings=settings)
    params: dict[str, str] = {
        "ids": "channel==MINE",
        "startDate": start_date.isoformat(),
        "endDate": end_date.isoformat(),
        "metrics": ",".join(metrics),
        "filters": f"video=={video_id}",
    }
    if dimensions:
        params["dimensions"] = dimensions
    response = httpx.get(
        ANALYTICS_URL,
        params=params,
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=30,
    )
    if response.is_error:
        raise YouTubeAnalyticsError(
            f"YouTube Analytics query failed ({response.status_code}): {response.text[:2000]}",
            status_code=response.status_code,
        )
    return dict(response.json())


def report_rows(payload: dict[str, Any]) -> list[dict[str, Any]]:
    headers = [str(item.get("name")) for item in payload.get("columnHeaders") or []]
    rows = payload.get("rows") or []
    return [dict(zip(headers, row, strict=False)) for row in rows]


def basic_video_metrics(
    connection_id: uuid.UUID,
    video_id: str,
    start_date: date,
    end_date: date,
    *,
    settings: Settings | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    try:
        payload = _query(
            connection_id,
            start_date=start_date,
            end_date=end_date,
            metrics=BASIC_METRICS,
            video_id=video_id,
            settings=settings,
        )
    except YouTubeAnalyticsError as exc:
        if exc.status_code != 400:
            raise
        payload = _query(
            connection_id,
            start_date=start_date,
            end_date=end_date,
            metrics=BASIC_METRICS_WITHOUT_ENGAGED,
            video_id=video_id,
            settings=settings,
        )
    rows = report_rows(payload)
    return (rows[0] if rows else {}), payload


def monetary_video_metrics(
    connection_id: uuid.UUID,
    video_id: str,
    start_date: date,
    end_date: date,
    *,
    settings: Settings | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    payload = _query(
        connection_id,
        start_date=start_date,
        end_date=end_date,
        metrics=MONETARY_METRICS,
        video_id=video_id,
        settings=settings,
    )
    rows = report_rows(payload)
    return (rows[0] if rows else {}), payload


def retention_curve(
    connection_id: uuid.UUID,
    video_id: str,
    start_date: date,
    end_date: date,
    *,
    settings: Settings | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    payload = _query(
        connection_id,
        start_date=start_date,
        end_date=end_date,
        metrics=RETENTION_METRICS,
        video_id=video_id,
        dimensions="elapsedVideoTimeRatio",
        settings=settings,
    )
    return report_rows(payload), payload
