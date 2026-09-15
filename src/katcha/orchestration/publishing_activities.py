from __future__ import annotations

import shutil
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import select
from temporalio import activity
from temporalio.exceptions import ApplicationError

from katcha.config import get_settings
from katcha.db import session_scope
from katcha.domain import PublicationStatus, ProductionStatus, YouTubeConnectionStatus
from katcha.integrations.storage import ObjectStore
from katcha.integrations.youtube.analytics import (
    YouTubeAnalyticsError,
    basic_video_metrics,
    monetary_video_metrics,
    retention_curve,
)
from katcha.integrations.youtube.client import (
    UploadSessionExpired,
    YouTubeClient,
    read_chunk,
)
from katcha.integrations.youtube.oauth import MONETARY_SCOPE
from katcha.models import DomainEvent
from katcha.production_models import Production, ProductionAsset
from katcha.publishing_models import (
    Publication,
    PublicationAnalyticsSnapshot,
    RetentionPoint,
    YouTubeConnection,
)
from katcha.security.secrets import decrypt_secret, encrypt_secret


def _render_asset(session: Any, production_id: uuid.UUID) -> ProductionAsset:
    asset = session.scalar(
        select(ProductionAsset).where(
            ProductionAsset.production_id == production_id,
            ProductionAsset.kind == "render",
            ProductionAsset.generation == 1,
        )
    )
    if asset is None:
        raise RuntimeError("publication production has no render asset")
    return asset


def _as_int(value: object) -> int | None:
    if value is None or value == "":
        return None
    return int(value)


def _as_decimal(value: object) -> Decimal | None:
    if value is None or value == "":
        return None
    return Decimal(str(value))


def _persist_completed_upload(
    publication_id: uuid.UUID,
    response: dict[str, Any],
) -> str:
    video_id = str(response.get("id") or "")
    if not video_id:
        raise RuntimeError("completed YouTube upload response did not include a video ID")
    with session_scope() as session:
        publication = session.get(Publication, publication_id)
        if publication is None:
            raise RuntimeError("publication disappeared after YouTube upload")
        publication.youtube_video_id = video_id
        publication.upload_offset = int(publication.upload_size or publication.upload_offset)
        publication.status = PublicationStatus.UPLOADED.value
        publication.stage = "uploaded"
        publication.error = None
        publication.raw_status = {
            **dict(publication.raw_status or {}),
            "upload_response": response,
        }
        session.add(
            DomainEvent(
                aggregate_type="publication",
                aggregate_id=str(publication_id),
                event_type="publication.uploaded",
                payload={
                    "publication_id": str(publication_id),
                    "youtube_video_id": video_id,
                },
            )
        )
    return video_id


@activity.defn
def prepare_publication_activity(publication_id: str) -> dict[str, object]:
    publication_uuid = uuid.UUID(publication_id)
    with session_scope() as session:
        publication = session.get(Publication, publication_uuid)
        if publication is None:
            raise ValueError(f"publication not found: {publication_id}")
        production = session.get(Production, publication.production_id)
        if production is None or production.status != ProductionStatus.APPROVED.value:
            raise RuntimeError("publication production is no longer approved")
        connection = session.get(YouTubeConnection, publication.youtube_connection_id)
        if connection is None or connection.status != YouTubeConnectionStatus.ACTIVE.value:
            raise RuntimeError("YouTube connection is not active")
        render = _render_asset(session, publication.production_id)
        publication.stage = "prepared"
        publication.error = None
        return {
            "publication_id": publication_id,
            "render_key": render.storage_key,
            "youtube_video_id": publication.youtube_video_id,
        }


@activity.defn
def initiate_upload_session_activity(publication_id: str) -> dict[str, object]:
    publication_uuid = uuid.UUID(publication_id)
    settings = get_settings()
    store = ObjectStore(settings)

    with session_scope() as session:
        publication = session.get(Publication, publication_uuid)
        if publication is None:
            raise ValueError(f"publication not found: {publication_id}")
        if publication.youtube_video_id:
            return {
                "publication_id": publication_id,
                "youtube_video_id": publication.youtube_video_id,
                "reused": True,
            }
        if publication.encrypted_upload_url and publication.upload_size:
            return {
                "publication_id": publication_id,
                "upload_size": publication.upload_size,
                "reused": True,
            }
        render = _render_asset(session, publication.production_id)
        render_key = render.storage_key
        connection_id = publication.youtube_connection_id
        title = publication.title
        description = publication.description
        tags = list(publication.tags or [])
        category_id = publication.category_id
        notify_subscribers = publication.notify_subscribers
        made_for_kids = publication.made_for_kids
        synthetic = publication.contains_synthetic_media

    stat = store.stat(render_key)
    size_bytes = int(stat["size_bytes"])
    if size_bytes <= 0:
        raise RuntimeError("render asset is empty")
    mime_type = str(stat.get("content_type") or "video/mp4")
    client = YouTubeClient(connection_id, settings=settings)
    upload_url = client.initiate_resumable_upload(
        title=title,
        description=description,
        tags=tags,
        category_id=category_id,
        size_bytes=size_bytes,
        mime_type=mime_type,
        notify_subscribers=notify_subscribers,
        made_for_kids=made_for_kids,
        contains_synthetic_media=synthetic,
    )

    with session_scope() as session:
        publication = session.get(Publication, publication_uuid)
        if publication is None:
            raise RuntimeError("publication disappeared after upload-session creation")
        if publication.encrypted_upload_url is None:
            publication.encrypted_upload_url = encrypt_secret(upload_url, settings)
            publication.upload_size = size_bytes
            publication.upload_offset = 0
        publication.status = PublicationStatus.UPLOADING.value
        publication.stage = "upload_session_ready"
        session.add(
            DomainEvent(
                aggregate_type="publication",
                aggregate_id=publication_id,
                event_type="publication.upload_session_created",
                payload={
                    "publication_id": publication_id,
                    "upload_size": size_bytes,
                    "workflow_attempt": publication.workflow_attempt,
                },
            )
        )
    return {"publication_id": publication_id, "upload_size": size_bytes, "reused": False}


@activity.defn
def upload_video_activity(publication_id: str) -> dict[str, object]:
    publication_uuid = uuid.UUID(publication_id)
    settings = get_settings()
    store = ObjectStore(settings)
    work_dir = settings.work_dir / "publishing" / publication_id
    video_path = work_dir / "video.mp4"

    with session_scope() as session:
        publication = session.get(Publication, publication_uuid)
        if publication is None:
            raise ValueError(f"publication not found: {publication_id}")
        if publication.youtube_video_id:
            return {
                "publication_id": publication_id,
                "youtube_video_id": publication.youtube_video_id,
                "reused": True,
            }
        if not publication.encrypted_upload_url or not publication.upload_size:
            raise RuntimeError("publication has no resumable upload session")
        render = _render_asset(session, publication.production_id)
        upload_url = decrypt_secret(publication.encrypted_upload_url, settings)
        total_size = int(publication.upload_size)
        connection_id = publication.youtube_connection_id
        render_key = render.storage_key

    client = YouTubeClient(connection_id, settings=settings)
    try:
        try:
            progress = client.query_upload(upload_url, total_size)
        except UploadSessionExpired as exc:
            with session_scope() as session:
                publication = session.get(Publication, publication_uuid)
                if publication is not None:
                    publication.status = PublicationStatus.FAILED.value
                    publication.stage = "upload_session_expired"
                    publication.error = str(exc)
            raise ApplicationError(str(exc), non_retryable=True) from exc

        if progress.complete:
            video_id = _persist_completed_upload(publication_uuid, progress.response or {})
            return {"publication_id": publication_id, "youtube_video_id": video_id, "reused": True}

        authoritative_offset = progress.offset
        with session_scope() as session:
            publication = session.get(Publication, publication_uuid)
            if publication is None:
                raise RuntimeError("publication disappeared during upload recovery")
            publication.upload_offset = authoritative_offset
            publication.status = PublicationStatus.UPLOADING.value
            publication.stage = "uploading"

        work_dir.mkdir(parents=True, exist_ok=True)
        store.download_file(render_key, video_path)
        if video_path.stat().st_size != total_size:
            raise RuntimeError(
                f"downloaded render size mismatch: expected {total_size}, got {video_path.stat().st_size}"
            )

        chunk_size = settings.youtube_upload_chunk_mb * 1024 * 1024
        offset = authoritative_offset
        while offset < total_size:
            data = read_chunk(video_path, offset, min(chunk_size, total_size - offset))
            if not data:
                raise RuntimeError("render ended before the expected upload size")
            try:
                progress = client.upload_chunk(
                    upload_url,
                    data=data,
                    start=offset,
                    total_size=total_size,
                    mime_type="video/mp4",
                )
            except UploadSessionExpired as exc:
                with session_scope() as session:
                    publication = session.get(Publication, publication_uuid)
                    if publication is not None:
                        publication.status = PublicationStatus.FAILED.value
                        publication.stage = "upload_session_expired"
                        publication.error = str(exc)
                raise ApplicationError(str(exc), non_retryable=True) from exc

            offset = progress.offset
            with session_scope() as session:
                publication = session.get(Publication, publication_uuid)
                if publication is None:
                    raise RuntimeError("publication disappeared while persisting upload progress")
                publication.upload_offset = offset
                publication.stage = "uploading"
            activity.heartbeat({"offset": offset, "total_size": total_size})

            if progress.complete:
                video_id = _persist_completed_upload(publication_uuid, progress.response or {})
                return {
                    "publication_id": publication_id,
                    "youtube_video_id": video_id,
                    "reused": False,
                }

        raise RuntimeError("YouTube upload reached total size without a completion response")
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


@activity.defn
def refresh_video_status_activity(publication_id: str) -> dict[str, object]:
    publication_uuid = uuid.UUID(publication_id)
    settings = get_settings()
    with session_scope() as session:
        publication = session.get(Publication, publication_uuid)
        if publication is None:
            raise ValueError(f"publication not found: {publication_id}")
        if not publication.youtube_video_id:
            raise RuntimeError("publication has no YouTube video ID")
        video_id = publication.youtube_video_id
        connection_id = publication.youtube_connection_id

    resource = YouTubeClient(connection_id, settings=settings).video_resource(video_id)
    status_data = dict(resource.get("status") or {})
    processing = dict(resource.get("processingDetails") or {})
    upload_status = str(status_data.get("uploadStatus") or "")
    processing_status = str(processing.get("processingStatus") or upload_status or "unknown")
    failed = processing_status in {"failed", "terminated"} or upload_status in {
        "failed",
        "rejected",
        "deleted",
    }
    complete = processing_status == "succeeded" or upload_status == "processed"
    failure_reason = str(
        processing.get("processingFailureReason") or status_data.get("failureReason") or ""
    )
    rejection_reason = str(status_data.get("rejectionReason") or "")

    with session_scope() as session:
        publication = session.get(Publication, publication_uuid)
        if publication is None:
            raise RuntimeError("publication disappeared during status refresh")
        publication.processing_status = processing_status
        publication.failure_reason = failure_reason or None
        publication.rejection_reason = rejection_reason or None
        publication.raw_status = {
            **dict(publication.raw_status or {}),
            "video": resource,
        }
        if failed:
            publication.status = PublicationStatus.FAILED.value
            publication.stage = "processing_failed"
            publication.error = failure_reason or rejection_reason or processing_status
        elif complete:
            publication.status = PublicationStatus.UPLOADED.value
            publication.stage = "processing_complete"
            publication.error = None
        else:
            publication.status = PublicationStatus.PROCESSING.value
            publication.stage = "processing"

    if failed:
        raise ApplicationError(
            failure_reason or rejection_reason or f"YouTube processing failed: {processing_status}",
            non_retryable=True,
        )
    return {
        "publication_id": publication_id,
        "processing_status": processing_status,
        "complete": complete,
    }


@activity.defn
def mark_processing_timeout_activity(publication_id: str) -> None:
    publication_uuid = uuid.UUID(publication_id)
    with session_scope() as session:
        publication = session.get(Publication, publication_uuid)
        if publication is None:
            return
        publication.status = PublicationStatus.PROCESSING.value
        publication.stage = "processing_timeout"
        publication.error = "YouTube processing did not complete within the configured poll window"
        session.add(
            DomainEvent(
                aggregate_type="publication",
                aggregate_id=publication_id,
                event_type="publication.processing_timeout",
                payload={"publication_id": publication_id},
            )
        )


@activity.defn
def finalize_publication_activity(publication_id: str) -> dict[str, object]:
    publication_uuid = uuid.UUID(publication_id)
    settings = get_settings()
    now = datetime.now(UTC)
    with session_scope() as session:
        publication = session.get(Publication, publication_uuid)
        if publication is None:
            raise ValueError(f"publication not found: {publication_id}")
        if not publication.youtube_video_id:
            raise RuntimeError("publication has no YouTube video ID")
        video_id = publication.youtube_video_id
        connection_id = publication.youtube_connection_id
        publish_at = publication.publish_at
        privacy_status = publication.privacy_status
        made_for_kids = publication.made_for_kids
        synthetic = publication.contains_synthetic_media

    client = YouTubeClient(connection_id, settings=settings)
    if publish_at is not None and publish_at > now:
        response = client.schedule_video(
            video_id,
            publish_at=publish_at,
            made_for_kids=made_for_kids,
            contains_synthetic_media=synthetic,
        )
        final_status = PublicationStatus.SCHEDULED.value
        stage = "scheduled"
        published_at = None
        analytics_anchor = publish_at
    elif privacy_status == "public" or publish_at is not None:
        response = client.set_privacy(
            video_id,
            privacy_status="public",
            made_for_kids=made_for_kids,
            contains_synthetic_media=synthetic,
        )
        final_status = PublicationStatus.PUBLISHED.value
        stage = "published"
        published_at = now
        analytics_anchor = now
    elif privacy_status == "unlisted":
        response = client.set_privacy(
            video_id,
            privacy_status="unlisted",
            made_for_kids=made_for_kids,
            contains_synthetic_media=synthetic,
        )
        final_status = PublicationStatus.UNLISTED.value
        stage = "unlisted"
        published_at = now
        analytics_anchor = now
    else:
        response = client.video_resource(video_id)
        final_status = PublicationStatus.PRIVATE.value
        stage = "private"
        published_at = None
        analytics_anchor = now

    with session_scope() as session:
        publication = session.get(Publication, publication_uuid)
        if publication is None:
            raise RuntimeError("publication disappeared during finalization")
        publication.status = final_status
        publication.stage = stage
        publication.published_at = published_at
        publication.error = None
        publication.raw_status = {
            **dict(publication.raw_status or {}),
            "finalize_response": response,
        }
        session.add(
            DomainEvent(
                aggregate_type="publication",
                aggregate_id=publication_id,
                event_type=f"publication.{stage}",
                payload={
                    "publication_id": publication_id,
                    "youtube_video_id": video_id,
                    "publish_at": publish_at.isoformat() if publish_at else None,
                },
            )
        )
    return {
        "publication_id": publication_id,
        "status": final_status,
        "analytics_anchor_epoch": analytics_anchor.timestamp(),
    }


@activity.defn
def collect_analytics_snapshot_activity(
    publication_id: str,
    sample_key: str,
) -> dict[str, object]:
    publication_uuid = uuid.UUID(publication_id)
    settings = get_settings()
    now = datetime.now(UTC)

    with session_scope() as session:
        existing = session.scalar(
            select(PublicationAnalyticsSnapshot).where(
                PublicationAnalyticsSnapshot.publication_id == publication_uuid,
                PublicationAnalyticsSnapshot.sample_key == sample_key,
            )
        )
        if existing is not None:
            return {
                "publication_id": publication_id,
                "snapshot_id": str(existing.id),
                "sample_key": sample_key,
                "reused": True,
            }
        publication = session.get(Publication, publication_uuid)
        if publication is None:
            raise ValueError(f"publication not found: {publication_id}")
        if not publication.youtube_video_id:
            raise RuntimeError("publication has no YouTube video ID")
        connection = session.get(YouTubeConnection, publication.youtube_connection_id)
        if connection is None:
            raise RuntimeError("publication YouTube connection no longer exists")
        connection_id = connection.id
        scopes = set(connection.scopes or [])
        video_id = publication.youtube_video_id
        anchor = publication.published_at or publication.publish_at or publication.created_at

    today = now.date()
    period_start = min(anchor.date(), today)
    metrics, raw_metrics = basic_video_metrics(
        connection_id,
        video_id,
        period_start,
        today,
        settings=settings,
    )
    raw_video = YouTubeClient(connection_id, settings=settings).video_resource(video_id)
    stats = dict(raw_video.get("statistics") or {})

    monetary: dict[str, Any] = {}
    raw_monetary: dict[str, Any] = {}
    if MONETARY_SCOPE in scopes:
        try:
            monetary, raw_monetary = monetary_video_metrics(
                connection_id,
                video_id,
                period_start,
                today,
                settings=settings,
            )
        except YouTubeAnalyticsError as exc:
            if exc.status_code not in {400, 403}:
                raise
            raw_monetary = {"unavailable": True, "error": str(exc)}

    retention_rows: list[dict[str, Any]] = []
    try:
        retention_rows, raw_retention = retention_curve(
            connection_id,
            video_id,
            period_start,
            today,
            settings=settings,
        )
    except YouTubeAnalyticsError as exc:
        if exc.status_code not in {400, 403}:
            raise
        raw_retention = {"unavailable": True, "error": str(exc)}
    raw_metrics = {**raw_metrics, "_retention": raw_retention}

    snapshot_id = uuid.uuid4()
    with session_scope() as session:
        existing = session.scalar(
            select(PublicationAnalyticsSnapshot).where(
                PublicationAnalyticsSnapshot.publication_id == publication_uuid,
                PublicationAnalyticsSnapshot.sample_key == sample_key,
            )
        )
        if existing is not None:
            return {
                "publication_id": publication_id,
                "snapshot_id": str(existing.id),
                "sample_key": sample_key,
                "reused": True,
            }
        snapshot = PublicationAnalyticsSnapshot(
            id=snapshot_id,
            publication_id=publication_uuid,
            sample_key=sample_key,
            sampled_at=now,
            period_start=period_start,
            period_end=today,
            views=_as_int(metrics.get("views")) or _as_int(stats.get("viewCount")),
            engaged_views=_as_int(metrics.get("engagedViews")),
            estimated_minutes_watched=_as_decimal(metrics.get("estimatedMinutesWatched")),
            average_view_duration=_as_decimal(metrics.get("averageViewDuration")),
            average_view_percentage=_as_decimal(metrics.get("averageViewPercentage")),
            likes=_as_int(metrics.get("likes")) or _as_int(stats.get("likeCount")),
            comments=_as_int(metrics.get("comments")) or _as_int(stats.get("commentCount")),
            shares=_as_int(metrics.get("shares")),
            subscribers_gained=_as_int(metrics.get("subscribersGained")),
            subscribers_lost=_as_int(metrics.get("subscribersLost")),
            estimated_revenue=_as_decimal(monetary.get("estimatedRevenue")),
            estimated_ad_revenue=_as_decimal(monetary.get("estimatedAdRevenue")),
            monetized_playbacks=_as_int(monetary.get("monetizedPlaybacks")),
            raw_metrics=raw_metrics,
            raw_monetary=raw_monetary,
            raw_video=raw_video,
        )
        session.add(snapshot)
        session.flush()
        for row in retention_rows:
            ratio = _as_decimal(row.get("elapsedVideoTimeRatio"))
            if ratio is None:
                continue
            session.add(
                RetentionPoint(
                    snapshot_id=snapshot.id,
                    elapsed_video_time_ratio=ratio,
                    audience_watch_ratio=_as_decimal(row.get("audienceWatchRatio")),
                    relative_retention_performance=_as_decimal(
                        row.get("relativeRetentionPerformance")
                    ),
                    raw_row=row,
                )
            )
        session.add(
            DomainEvent(
                aggregate_type="publication",
                aggregate_id=publication_id,
                event_type="publication.analytics_sampled",
                payload={
                    "publication_id": publication_id,
                    "snapshot_id": str(snapshot.id),
                    "sample_key": sample_key,
                    "retention_points": len(retention_rows),
                },
            )
        )
    return {
        "publication_id": publication_id,
        "snapshot_id": str(snapshot_id),
        "sample_key": sample_key,
        "reused": False,
    }


@activity.defn
def mark_analytics_observation_failed(publication_id: str, message: str) -> None:
    publication_uuid = uuid.UUID(publication_id)
    with session_scope() as session:
        publication = session.get(Publication, publication_uuid)
        if publication is None:
            return
        publication.raw_status = {
            **dict(publication.raw_status or {}),
            "analytics_error": message[:2000],
        }
        session.add(
            DomainEvent(
                aggregate_type="publication",
                aggregate_id=publication_id,
                event_type="publication.analytics_failed",
                payload={"publication_id": publication_id, "error": message[:2000]},
            )
        )


@activity.defn
def mark_publication_failed(publication_id: str, message: str) -> None:
    publication_uuid = uuid.UUID(publication_id)
    with session_scope() as session:
        publication = session.get(Publication, publication_uuid)
        if publication is None:
            return
        if publication.status != PublicationStatus.FAILED.value:
            publication.status = PublicationStatus.FAILED.value
            publication.stage = "failed"
        publication.error = message[:8000]
        session.add(
            DomainEvent(
                aggregate_type="publication",
                aggregate_id=publication_id,
                event_type="publication.failed",
                payload={"publication_id": publication_id, "error": publication.error},
            )
        )
