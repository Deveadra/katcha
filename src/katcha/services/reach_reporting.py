from __future__ import annotations

import csv
import hashlib
import io
import uuid
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select

from katcha.db import session_scope
from katcha.integrations.youtube.reporting import (
    ANALYTICS_SCOPE,
    MONETARY_SCOPE,
    REACH_REPORT_TYPE,
)
from katcha.models import DomainEvent
from katcha.packaging_models import PublicationPackagingActivation
from katcha.publishing_models import Publication, YouTubeConnection
from katcha.reach_models import (
    PublicationReachObservation,
    YouTubeReachReportingJob,
    YouTubeReachReportImport,
)

PACIFIC = ZoneInfo("America/Los_Angeles")
REQUIRED_COLUMNS = {
    "date",
    "channel_id",
    "video_id",
    "video_thumbnail_impressions",
    "video_thumbnail_impressions_ctr",
}
MAX_REPORT_ROWS = 100_000


def _parse_provider_datetime(value: object) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _optional_int(value: object) -> int | None:
    raw = str(value or "").strip()
    return int(raw) if raw else None


def _optional_decimal(value: object) -> Decimal | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        return Decimal(raw)
    except InvalidOperation as exc:
        raise ValueError(f"invalid reach decimal value: {raw[:80]}") from exc


def parse_reach_csv(payload: bytes) -> list[dict[str, str]]:
    try:
        text_payload = payload.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError("YouTube reach report is not UTF-8 CSV") from exc
    reader = csv.DictReader(io.StringIO(text_payload))
    fields = set(reader.fieldnames or [])
    if not REQUIRED_COLUMNS <= fields:
        missing = sorted(REQUIRED_COLUMNS - fields)
        raise ValueError(f"YouTube reach report is missing required columns: {missing}")
    rows: list[dict[str, str]] = []
    for index, raw in enumerate(reader, start=1):
        if index > MAX_REPORT_ROWS:
            raise ValueError("YouTube reach report exceeds the row limit")
        rows.append({str(key): str(value or "") for key, value in raw.items()})
    return rows


def ensure_local_reach_job(connection_id: uuid.UUID) -> YouTubeReachReportingJob:
    with session_scope() as session:
        connection = session.get(YouTubeConnection, connection_id)
        if connection is None:
            raise ValueError(f"YouTube connection not found: {connection_id}")
        scopes = set(connection.scopes or [])
        if ANALYTICS_SCOPE not in scopes and MONETARY_SCOPE not in scopes:
            raise ValueError("YouTube connection lacks an analytics reporting scope")
        row = session.scalar(
            select(YouTubeReachReportingJob).where(
                YouTubeReachReportingJob.youtube_connection_id == connection_id,
                YouTubeReachReportingJob.report_type_id == REACH_REPORT_TYPE,
            )
        )
        if row is None:
            row = YouTubeReachReportingJob(
                youtube_connection_id=connection_id,
                report_type_id=REACH_REPORT_TYPE,
                status="registered",
                stage="registered",
            )
            session.add(row)
            session.flush()
        session.refresh(row)
        session.expunge(row)
        return row


def mark_job_create_started(job_id: uuid.UUID) -> None:
    with session_scope() as session:
        row = session.get(YouTubeReachReportingJob, job_id)
        if row is None:
            raise ValueError(f"reach reporting job not found: {job_id}")
        if row.provider_job_id:
            return
        if row.stage == "provider_create_started":
            raise RuntimeError(
                "reporting job creation is ambiguous; reconcile provider jobs before retry"
            )
        row.status = "create_started"
        row.stage = "provider_create_started"
        row.error = None


def activate_reach_job(
    job_id: uuid.UUID,
    *,
    provider_job_id: str,
    provider_payload: dict[str, Any],
) -> YouTubeReachReportingJob:
    with session_scope() as session:
        row = session.get(YouTubeReachReportingJob, job_id)
        if row is None:
            raise ValueError(f"reach reporting job not found: {job_id}")
        row.provider_job_id = provider_job_id
        row.status = "active"
        row.stage = "active"
        row.error = None
        row.job_metadata = dict(provider_payload)
        session.add(
            DomainEvent(
                aggregate_type="youtube_connection",
                aggregate_id=str(row.youtube_connection_id),
                event_type="youtube.reach_reporting_job_active",
                payload={
                    "connection_id": str(row.youtube_connection_id),
                    "reach_job_id": str(row.id),
                    "provider_job_id": provider_job_id,
                    "report_type_id": row.report_type_id,
                },
            )
        )
        session.flush()
        session.refresh(row)
        session.expunge(row)
        return row


def _attribution(
    session: object,
    publication_id: uuid.UUID,
    report_date: date,
) -> tuple[str, uuid.UUID | None, dict[str, object]]:
    day_start = datetime.combine(report_date, time.min, PACIFIC).astimezone(UTC)
    day_end = datetime.combine(
        report_date + timedelta(days=1),
        time.min,
        PACIFIC,
    ).astimezone(UTC)
    activations = list(
        session.scalars(
            select(PublicationPackagingActivation)
            .where(
                PublicationPackagingActivation.publication_id == publication_id,
                PublicationPackagingActivation.status == "applied",
                PublicationPackagingActivation.applied_at.is_not(None),
                PublicationPackagingActivation.applied_at < day_end,
            )
            .order_by(PublicationPackagingActivation.applied_at)
        )
    )
    def applied_at_utc(row: PublicationPackagingActivation) -> datetime | None:
        value = row.applied_at
        if value is None:
            return None
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value.astimezone(UTC)

    within_day = [
        row for row in activations
        if applied_at_utc(row) is not None
        and day_start <= applied_at_utc(row) < day_end
    ]
    if within_day:
        return (
            "mixed",
            None,
            {
                "report_timezone": "America/Los_Angeles",
                "day_start_utc": day_start.isoformat(),
                "day_end_utc": day_end.isoformat(),
                "activation_ids": [str(row.id) for row in within_day],
                "reason": "packaging_changed_within_report_day",
            },
        )
    active_before = [
        row for row in activations
        if applied_at_utc(row) is not None and applied_at_utc(row) < day_start
    ]
    if not active_before:
        return (
            "legacy",
            None,
            {
                "report_timezone": "America/Los_Angeles",
                "day_start_utc": day_start.isoformat(),
                "day_end_utc": day_end.isoformat(),
                "reason": "no_katcha_packaging_active_before_report_day",
            },
        )
    active = active_before[-1]
    return (
        "variant",
        active.variant_id,
        {
            "report_timezone": "America/Los_Angeles",
            "day_start_utc": day_start.isoformat(),
            "day_end_utc": day_end.isoformat(),
            "activation_id": str(active.id),
            "applied_at": (
                applied_at_utc(active).isoformat()
                if applied_at_utc(active) is not None
                else None
            ),
        },
    )


def import_reach_report(
    connection_id: uuid.UUID,
    job_id: uuid.UUID,
    *,
    report: dict[str, Any],
    payload: bytes,
) -> dict[str, object]:
    provider_report_id = str(report.get("id") or "").strip()
    if not provider_report_id:
        raise ValueError("YouTube report metadata has no report ID")
    payload_sha256 = hashlib.sha256(payload).hexdigest()
    rows = parse_reach_csv(payload)

    with session_scope() as session:
        existing_import = session.scalar(
            select(YouTubeReachReportImport).where(
                YouTubeReachReportImport.youtube_connection_id == connection_id,
                YouTubeReachReportImport.provider_report_id == provider_report_id,
            )
        )
        if existing_import is not None:
            if existing_import.payload_sha256 != payload_sha256:
                raise ValueError("provider report ID was reused with different bytes")
            return {
                "report_id": provider_report_id,
                "imported": False,
                "row_count": existing_import.row_count,
                "observations_created": 0,
            }

        connection = session.get(YouTubeConnection, connection_id)
        if connection is None:
            raise ValueError(f"YouTube connection not found: {connection_id}")
        import_row = YouTubeReachReportImport(
            youtube_connection_id=connection_id,
            reach_job_id=job_id,
            provider_report_id=provider_report_id,
            report_start=_parse_provider_datetime(report.get("startTime")),
            report_end=_parse_provider_datetime(report.get("endTime")),
            provider_created_at=_parse_provider_datetime(report.get("createTime")),
            payload_sha256=payload_sha256,
            row_count=len(rows),
            import_metadata={
                "job_id": str(report.get("jobId") or ""),
                "report_type_id": REACH_REPORT_TYPE,
            },
        )
        session.add(import_row)
        session.flush()

        created = 0
        ignored = 0
        for raw in rows:
            if raw["channel_id"] != connection.channel_id:
                ignored += 1
                continue
            publication = session.scalar(
                select(Publication).where(
                    Publication.youtube_connection_id == connection_id,
                    Publication.youtube_video_id == raw["video_id"],
                )
            )
            if publication is None:
                ignored += 1
                continue
            report_date = date.fromisoformat(raw["date"])
            existing = session.scalar(
                select(PublicationReachObservation).where(
                    PublicationReachObservation.publication_id == publication.id,
                    PublicationReachObservation.report_date == report_date,
                )
            )
            impressions = _optional_int(raw["video_thumbnail_impressions"])
            ctr = _optional_decimal(raw["video_thumbnail_impressions_ctr"])
            if existing is not None:
                if existing.impressions != impressions or existing.ctr != ctr:
                    raise ValueError(
                        "overlapping reach reports disagree for the same publication/day"
                    )
                ignored += 1
                continue
            status, variant_id, attribution_metadata = _attribution(
                session,
                publication.id,
                report_date,
            )
            session.add(
                PublicationReachObservation(
                    publication_id=publication.id,
                    report_import_id=import_row.id,
                    report_date=report_date,
                    impressions=impressions,
                    ctr=ctr,
                    attribution_status=status,
                    packaging_variant_id=variant_id,
                    attribution_metadata=attribution_metadata,
                    raw_row=dict(raw),
                )
            )
            created += 1

        session.add(
            DomainEvent(
                aggregate_type="youtube_connection",
                aggregate_id=str(connection_id),
                event_type="youtube.reach_report_imported",
                payload={
                    "connection_id": str(connection_id),
                    "provider_report_id": provider_report_id,
                    "row_count": len(rows),
                    "observations_created": created,
                    "rows_ignored": ignored,
                    "payload_sha256": payload_sha256,
                },
            )
        )
        return {
            "report_id": provider_report_id,
            "imported": True,
            "row_count": len(rows),
            "observations_created": created,
            "rows_ignored": ignored,
        }


def reach_observations_for_publication(
    publication_id: uuid.UUID,
) -> list[PublicationReachObservation]:
    with session_scope() as session:
        if session.get(Publication, publication_id) is None:
            raise ValueError(f"publication not found: {publication_id}")
        rows = list(
            session.scalars(
                select(PublicationReachObservation)
                .where(PublicationReachObservation.publication_id == publication_id)
                .order_by(PublicationReachObservation.report_date)
            )
        )
        for row in rows:
            session.expunge(row)
        return rows



def reach_imports_for_connection(
    connection_id: uuid.UUID,
) -> list[YouTubeReachReportImport]:
    with session_scope() as session:
        if session.get(YouTubeConnection, connection_id) is None:
            raise ValueError(f"YouTube connection not found: {connection_id}")
        rows = list(
            session.scalars(
                select(YouTubeReachReportImport)
                .where(YouTubeReachReportImport.youtube_connection_id == connection_id)
                .order_by(YouTubeReachReportImport.imported_at.desc())
            )
        )
        for row in rows:
            session.expunge(row)
        return rows


def reach_summary_for_publication(
    publication_id: uuid.UUID,
    *,
    maturity_days: int = 7,
) -> dict[str, object]:
    if maturity_days < 1 or maturity_days > 90:
        raise ValueError("maturity_days must be between 1 and 90")
    with session_scope() as session:
        if session.get(Publication, publication_id) is None:
            raise ValueError(f"publication not found: {publication_id}")
        rows = list(
            session.scalars(
                select(PublicationReachObservation)
                .where(PublicationReachObservation.publication_id == publication_id)
                .order_by(PublicationReachObservation.report_date)
            )
        )

    grouped: dict[uuid.UUID, list[PublicationReachObservation]] = {}
    mixed_days = 0
    legacy_days = 0
    for row in rows:
        if row.attribution_status == "mixed":
            mixed_days += 1
            continue
        if row.attribution_status == "legacy":
            legacy_days += 1
            continue
        if row.packaging_variant_id is None:
            continue
        grouped.setdefault(row.packaging_variant_id, []).append(row)

    variants: list[dict[str, object]] = []
    for variant_id, attributed_rows in grouped.items():
        window = attributed_rows[:maturity_days]
        known_impressions = [
            row for row in window if row.impressions is not None
        ]
        impressions = sum(int(row.impressions or 0) for row in known_impressions)
        ctr_weighted_rows = [
            row
            for row in window
            if row.impressions is not None and row.ctr is not None
        ]
        ctr_weight = sum(int(row.impressions or 0) for row in ctr_weighted_rows)
        weighted_ctr = (
            sum(
                Decimal(int(row.impressions or 0)) * Decimal(row.ctr)
                for row in ctr_weighted_rows
            )
            / Decimal(ctr_weight)
            if ctr_weight > 0
            else None
        )
        variants.append(
            {
                "variant_id": str(variant_id),
                "maturity_days": maturity_days,
                "observed_full_days": len(window),
                "impression_covered_days": len(known_impressions),
                "ctr_covered_days": len(ctr_weighted_rows),
                "impressions": impressions if known_impressions else None,
                "weighted_ctr": str(weighted_ctr) if weighted_ctr is not None else None,
                "first_report_date": (
                    window[0].report_date.isoformat() if window else None
                ),
                "last_report_date": (
                    window[-1].report_date.isoformat() if window else None
                ),
            }
        )

    variants.sort(key=lambda item: str(item["variant_id"]))
    return {
        "publication_id": str(publication_id),
        "maturity_days": maturity_days,
        "report_days": len(rows),
        "mixed_days": mixed_days,
        "legacy_days": legacy_days,
        "variants": variants,
    }
