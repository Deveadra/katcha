from __future__ import annotations

import hashlib
import uuid
from collections import defaultdict
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select

from katcha.db import session_scope
from katcha.integrations.youtube.analytics import (
    YouTubeAnalyticsError,
    basic_video_metrics,
    monetary_video_metrics,
    retention_curve,
)
from katcha.integrations.youtube.oauth import MONETARY_SCOPE
from katcha.intelligence_models import ChannelProfile
from katcha.longform_models import Compilation
from katcha.models import DomainEvent
from katcha.packaging_intelligence_models import (
    PackagingIntelligenceSnapshot,
    PackagingVariantPerformanceWindow,
)
from katcha.packaging_models import (
    PublicationPackagingActivation,
    PublicationPackagingVariant,
)
from katcha.production_models import Production
from katcha.publishing_models import (
    Publication,
    PublicationAnalyticsSnapshot,
    YouTubeConnection,
)
from katcha.reach_models import PublicationReachObservation
from katcha.short_episode_models import ShortEpisode

_DEFAULT_MATURITY_DAYS = 7
_ALLOWED_MATURITY_DAYS = (3, 7, 14)
_MIN_IMPRESSIONS = 1_000
_MIN_CTR_RELATIVE_UPLIFT = 0.05
_MAX_AVG_VIEW_PCT_DROP = 3.0
_MAX_RETENTION_50_DROP = 0.05
_RETENTION_TARGET = 0.50
_RETENTION_MAX_DISTANCE = 0.08
_MIN_CHRONOLOGICAL_COMPARISONS = 10
_HOLDOUT_FRACTION = 0.20


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _number(value: object) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _decimal(value: object) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except Exception:
        return None


def _production_cost(session: object, source: Production) -> Decimal:
    total = Decimal("0")
    current: Production | None = source
    seen: set[uuid.UUID] = set()
    while current is not None and current.id not in seen:
        seen.add(current.id)
        total += Decimal(current.estimated_cost_usd or 0)
        if current.parent_production_id is None:
            break
        current = session.get(Production, current.parent_production_id)
    return total


def _episode_cost(session: object, source: ShortEpisode) -> Decimal:
    total = Decimal("0")
    current: ShortEpisode | None = source
    seen: set[uuid.UUID] = set()
    while current is not None and current.id not in seen:
        seen.add(current.id)
        total += Decimal(current.estimated_cost_usd or 0)
        if current.parent_episode_id is None:
            break
        current = session.get(ShortEpisode, current.parent_episode_id)
    return total


def _compilation_cost(session: object, source: Compilation) -> Decimal:
    total = Decimal("0")
    current: Compilation | None = source
    seen: set[uuid.UUID] = set()
    while current is not None and current.id not in seen:
        seen.add(current.id)
        total += Decimal(current.estimated_cost_usd or 0)
        if current.parent_compilation_id is None:
            break
        current = session.get(Compilation, current.parent_compilation_id)
    return total


def _publication_cost(session: object, publication: Publication) -> Decimal:
    if publication.production_id is not None:
        source = session.get(Production, publication.production_id)
        return _production_cost(session, source) if source is not None else Decimal("0")
    if publication.short_episode_id is not None:
        source = session.get(ShortEpisode, publication.short_episode_id)
        return _episode_cost(session, source) if source is not None else Decimal("0")
    if publication.compilation_id is not None:
        source = session.get(Compilation, publication.compilation_id)
        return _compilation_cost(session, source) if source is not None else Decimal("0")
    return Decimal("0")


def _latest_publication_revenue(
    session: object,
    publication_id: uuid.UUID,
) -> Decimal | None:
    snapshot = session.scalar(
        select(PublicationAnalyticsSnapshot)
        .where(PublicationAnalyticsSnapshot.publication_id == publication_id)
        .order_by(PublicationAnalyticsSnapshot.sampled_at.desc())
        .limit(1)
    )
    if snapshot is None or snapshot.estimated_revenue is None:
        return None
    return Decimal(snapshot.estimated_revenue)


def _retention_value(rows: list[dict[str, Any]], target: float) -> float | None:
    candidates: list[tuple[float, float]] = []
    for row in rows:
        ratio = _number(row.get("elapsedVideoTimeRatio"))
        watch = _number(row.get("audienceWatchRatio"))
        if ratio is None or watch is None:
            continue
        candidates.append((ratio, watch))
    if not candidates:
        return None
    ratio, watch = min(candidates, key=lambda item: abs(item[0] - target))
    if abs(ratio - target) > _RETENTION_MAX_DISTANCE:
        return None
    return watch


def _consecutive_runs(
    rows: list[PublicationReachObservation],
) -> list[list[PublicationReachObservation]]:
    ordered = sorted(rows, key=lambda row: row.report_date)
    if not ordered:
        return []
    runs: list[list[PublicationReachObservation]] = [[ordered[0]]]
    for row in ordered[1:]:
        previous = runs[-1][-1]
        if row.report_date == previous.report_date + timedelta(days=1):
            runs[-1].append(row)
        else:
            runs.append([row])
    return runs


def _eligible_reach_windows(
    rows: list[PublicationReachObservation],
    maturity_days: int,
) -> list[list[PublicationReachObservation]]:
    by_variant: dict[uuid.UUID, list[PublicationReachObservation]] = defaultdict(list)
    for row in rows:
        if row.attribution_status != "variant" or row.packaging_variant_id is None:
            continue
        by_variant[row.packaging_variant_id].append(row)

    result: list[list[PublicationReachObservation]] = []
    for variant_rows in by_variant.values():
        for run in _consecutive_runs(variant_rows):
            for offset in range(0, len(run), maturity_days):
                window = run[offset : offset + maturity_days]
                if len(window) == maturity_days:
                    result.append(window)
    result.sort(key=lambda window: (window[0].report_date, str(window[0].packaging_variant_id)))
    return result


def _reach_metrics(
    rows: list[PublicationReachObservation],
) -> tuple[int | None, Decimal | None, dict[str, object]]:
    impression_rows = [row for row in rows if row.impressions is not None]
    ctr_rows = [
        row
        for row in rows
        if row.impressions is not None and row.ctr is not None
    ]
    full_impression_coverage = len(impression_rows) == len(rows)
    full_ctr_coverage = len(ctr_rows) == len(rows)
    impressions = (
        sum(int(row.impressions or 0) for row in impression_rows)
        if full_impression_coverage
        else None
    )
    weight = sum(int(row.impressions or 0) for row in ctr_rows)
    ctr = (
        sum(
            Decimal(int(row.impressions or 0)) * Decimal(row.ctr)
            for row in ctr_rows
        )
        / Decimal(weight)
        if full_ctr_coverage and weight > 0
        else None
    )
    return impressions, ctr, {
        "reach_days": len(rows),
        "impression_covered_days": len(impression_rows),
        "ctr_covered_days": len(ctr_rows),
        "full_impression_coverage": full_impression_coverage,
        "full_ctr_coverage": full_ctr_coverage,
    }


def _evidence_key(
    rows: list[PublicationReachObservation],
    maturity_days: int,
) -> str:
    payload = "|".join(
        [
            str(maturity_days),
            *[
                ":".join(
                    [
                        str(row.id),
                        row.report_date.isoformat(),
                        str(row.impressions) if row.impressions is not None else "missing",
                        str(row.ctr) if row.ctr is not None else "missing",
                    ]
                )
                for row in rows
            ],
        ]
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _measure_window(
    session: object,
    *,
    publication: Publication,
    variant: PublicationPackagingVariant,
    rows: list[PublicationReachObservation],
    maturity_days: int,
    connection: YouTubeConnection,
) -> PackagingVariantPerformanceWindow | None:
    evidence_key = _evidence_key(rows, maturity_days)
    existing = session.scalar(
        select(PackagingVariantPerformanceWindow).where(
            PackagingVariantPerformanceWindow.packaging_variant_id == variant.id,
            PackagingVariantPerformanceWindow.evidence_key == evidence_key,
        )
    )
    if existing is not None:
        return existing
    if not publication.youtube_video_id:
        return None

    window_start = rows[0].report_date
    window_end = rows[-1].report_date
    impressions, ctr, reach_metadata = _reach_metrics(rows)

    basic, raw_basic = basic_video_metrics(
        connection.id,
        publication.youtube_video_id,
        window_start,
        window_end,
    )
    if not basic:
        return None

    retention_rows: list[dict[str, Any]] = []
    raw_retention: dict[str, Any] = {}
    try:
        retention_rows, raw_retention = retention_curve(
            connection.id,
            publication.youtube_video_id,
            window_start,
            window_end,
        )
    except YouTubeAnalyticsError as exc:
        if exc.status_code not in {400, 403}:
            raise
        raw_retention = {"unavailable": True, "error": str(exc)}

    scopes = set(connection.scopes or [])
    monetary_scope = MONETARY_SCOPE in scopes
    interval_revenue: Decimal | None = None
    raw_monetary: dict[str, Any] = {}
    if monetary_scope:
        try:
            monetary, raw_monetary = monetary_video_metrics(
                connection.id,
                publication.youtube_video_id,
                window_start,
                window_end,
            )
            interval_revenue = _decimal(monetary.get("estimatedRevenue"))
        except YouTubeAnalyticsError as exc:
            if exc.status_code not in {400, 403}:
                raise
            raw_monetary = {"unavailable": True, "error": str(exc)}

    cost = _publication_cost(session, publication)
    publication_revenue = _latest_publication_revenue(session, publication.id)
    publication_margin = (
        publication_revenue - cost if publication_revenue is not None else None
    )
    row = PackagingVariantPerformanceWindow(
        publication_id=publication.id,
        packaging_variant_id=variant.id,
        evidence_key=evidence_key,
        maturity_days=maturity_days,
        window_start=window_start,
        window_end=window_end,
        impressions=impressions,
        ctr=ctr,
        views=(
            int(basic["views"])
            if basic.get("views") not in {None, ""}
            else None
        ),
        average_view_percentage=_decimal(basic.get("averageViewPercentage")),
        retention_50=_decimal(
            _retention_value(retention_rows, _RETENTION_TARGET)
        ),
        interval_revenue_usd=interval_revenue,
        publication_cost_usd=cost,
        publication_revenue_usd=publication_revenue,
        publication_contribution_margin_usd=publication_margin,
        evidence_status="measured",
        evidence_metadata={
            **reach_metadata,
            "reach_observation_ids": [str(item.id) for item in rows],
            "analytics_period_start": window_start.isoformat(),
            "analytics_period_end": window_end.isoformat(),
            "analytics_timezone": "America/Los_Angeles",
            "monetary_scope_available": monetary_scope,
            "raw_basic_column_count": len(raw_basic.get("columnHeaders") or []),
            "retention_point_count": len(retention_rows),
            "retention_unavailable": bool(raw_retention.get("unavailable")),
            "monetary_unavailable": bool(raw_monetary.get("unavailable")),
        },
    )
    session.add(row)
    session.flush()
    return row


def _window_payload(
    row: PackagingVariantPerformanceWindow,
    variant: PublicationPackagingVariant,
) -> dict[str, object]:
    metadata = dict(row.evidence_metadata or {})
    return {
        "window_id": str(row.id),
        "publication_id": str(row.publication_id),
        "variant_id": str(row.packaging_variant_id),
        "variant_key": variant.variant_key,
        "variant_version": variant.version,
        "title": variant.title,
        "has_thumbnail": variant.thumbnail_storage_key is not None,
        "maturity_days": row.maturity_days,
        "window_start": row.window_start.isoformat(),
        "window_end": row.window_end.isoformat(),
        "impressions": row.impressions,
        "ctr": float(row.ctr) if row.ctr is not None else None,
        "views": row.views,
        "average_view_percentage": (
            float(row.average_view_percentage)
            if row.average_view_percentage is not None
            else None
        ),
        "retention_50": (
            float(row.retention_50) if row.retention_50 is not None else None
        ),
        "interval_revenue_usd": (
            str(row.interval_revenue_usd)
            if row.interval_revenue_usd is not None
            else None
        ),
        "publication_cost_usd": str(row.publication_cost_usd),
        "publication_revenue_usd": (
            str(row.publication_revenue_usd)
            if row.publication_revenue_usd is not None
            else None
        ),
        "publication_contribution_margin_usd": (
            str(row.publication_contribution_margin_usd)
            if row.publication_contribution_margin_usd is not None
            else None
        ),
        "monetary_scope_available": bool(
            metadata.get("monetary_scope_available")
        ),
        "reach_coverage_complete": bool(
            metadata.get("full_impression_coverage")
            and metadata.get("full_ctr_coverage")
        ),
    }


def _comparison(
    baseline: dict[str, object],
    candidate: dict[str, object],
) -> dict[str, object]:
    blockers: list[str] = []
    info: list[str] = []

    baseline_impressions = baseline.get("impressions")
    candidate_impressions = candidate.get("impressions")
    baseline_ctr = _number(baseline.get("ctr"))
    candidate_ctr = _number(candidate.get("ctr"))
    baseline_view = _number(baseline.get("average_view_percentage"))
    candidate_view = _number(candidate.get("average_view_percentage"))
    baseline_retention = _number(baseline.get("retention_50"))
    candidate_retention = _number(candidate.get("retention_50"))

    if baseline_impressions is None or candidate_impressions is None:
        blockers.append("impressions_missing")
    elif (
        int(baseline_impressions) < _MIN_IMPRESSIONS
        or int(candidate_impressions) < _MIN_IMPRESSIONS
    ):
        blockers.append("impressions_below_floor")
    if baseline_ctr is None or candidate_ctr is None:
        blockers.append("ctr_missing")
    if baseline_view is None or candidate_view is None:
        blockers.append("average_view_percentage_missing")
    if baseline_retention is None or candidate_retention is None:
        blockers.append("retention_50_missing")

    ctr_relative_uplift: float | None = None
    if baseline_ctr is not None and candidate_ctr is not None and baseline_ctr > 0:
        ctr_relative_uplift = round(
            (candidate_ctr - baseline_ctr) / baseline_ctr,
            6,
        )
        if ctr_relative_uplift < _MIN_CTR_RELATIVE_UPLIFT:
            blockers.append("ctr_uplift_below_floor")
    elif baseline_ctr == 0 and candidate_ctr is not None:
        ctr_relative_uplift = None
        blockers.append("baseline_ctr_zero")

    view_delta: float | None = None
    if baseline_view is not None and candidate_view is not None:
        view_delta = round(candidate_view - baseline_view, 6)
        if view_delta < -_MAX_AVG_VIEW_PCT_DROP:
            blockers.append("watch_percentage_degraded")

    retention_delta: float | None = None
    if baseline_retention is not None and candidate_retention is not None:
        retention_delta = round(candidate_retention - baseline_retention, 6)
        if retention_delta < -_MAX_RETENTION_50_DROP:
            blockers.append("midpoint_retention_degraded")

    monetary_scope = bool(candidate.get("monetary_scope_available"))
    margin = _decimal(candidate.get("publication_contribution_margin_usd"))
    if monetary_scope:
        if margin is None:
            blockers.append("publication_margin_missing")
        elif margin < 0:
            blockers.append("publication_margin_negative")
    else:
        info.append("monetary_scope_unavailable")

    recommendation_type = "prefer" if not blockers else "observe"
    return {
        "publication_id": candidate["publication_id"],
        "baseline_variant_id": baseline["variant_id"],
        "candidate_variant_id": candidate["variant_id"],
        "recommendation_type": recommendation_type,
        "blockers": sorted(set(blockers)),
        "notes": info,
        "baseline_window_id": baseline["window_id"],
        "candidate_window_id": candidate["window_id"],
        "candidate_window_end": candidate["window_end"],
        "observed_delta": {
            "ctr_relative_uplift": ctr_relative_uplift,
            "average_view_percentage": view_delta,
            "retention_50": retention_delta,
        },
    }


def _recommendations(
    publications: list[Publication],
    variants_by_publication: dict[
        uuid.UUID,
        list[PublicationPackagingVariant],
    ],
    measured: list[dict[str, object]],
) -> list[dict[str, object]]:
    windows_by_publication: dict[str, list[dict[str, object]]] = defaultdict(list)
    for row in measured:
        windows_by_publication[str(row["publication_id"])].append(row)

    result: list[dict[str, object]] = []
    for publication in publications:
        publication_id = str(publication.id)
        windows = sorted(
            windows_by_publication.get(publication_id, []),
            key=lambda row: (str(row["window_start"]), str(row["variant_id"])),
        )
        if not windows:
            continue
        baseline = windows[0]
        measured_variant_ids = {str(row["variant_id"]) for row in windows}
        for candidate in windows[1:]:
            if candidate["variant_id"] == baseline["variant_id"]:
                continue
            result.append(_comparison(baseline, candidate))

        baseline_ready = (
            baseline.get("impressions") is not None
            and int(baseline["impressions"]) >= _MIN_IMPRESSIONS
            and baseline.get("ctr") is not None
            and baseline.get("average_view_percentage") is not None
            and baseline.get("retention_50") is not None
        )
        if baseline_ready:
            for variant in variants_by_publication.get(publication.id, []):
                if str(variant.id) in measured_variant_ids:
                    continue
                result.append(
                    {
                        "publication_id": publication_id,
                        "baseline_variant_id": baseline["variant_id"],
                        "candidate_variant_id": str(variant.id),
                        "recommendation_type": "test",
                        "blockers": [],
                        "notes": ["candidate_has_no_mature_measurement"],
                        "baseline_window_id": baseline["window_id"],
                        "candidate_window_id": None,
                        "candidate_window_end": None,
                        "observed_delta": {},
                    }
                )
    return result


def _chronological_validation(
    recommendations: list[dict[str, object]],
) -> dict[str, object]:
    comparisons = sorted(
        [
            row
            for row in recommendations
            if row.get("candidate_window_end") is not None
        ],
        key=lambda row: str(row["candidate_window_end"]),
    )
    count = len(comparisons)
    holdout_count = (
        max(1, int(round(count * _HOLDOUT_FRACTION)))
        if count >= _MIN_CHRONOLOGICAL_COMPARISONS
        else 0
    )
    development_count = count - holdout_count
    holdout = comparisons[development_count:] if holdout_count else []
    return {
        "method": "chronological_rule_audit",
        "comparison_count": count,
        "development_count": development_count,
        "holdout_count": holdout_count,
        "chronological_cutoff": (
            comparisons[development_count - 1]["candidate_window_end"]
            if development_count > 0 and holdout_count > 0
            else None
        ),
        "holdout_prefer_count": sum(
            row.get("recommendation_type") == "prefer" for row in holdout
        ),
        "optimizer_trained": False,
        "optimizer_reliable": False,
        "reliability_reason": (
            "rule_audit_only_no_trained_optimizer"
            if count >= _MIN_CHRONOLOGICAL_COMPARISONS
            else "insufficient_chronological_comparisons"
        ),
    }


def _policy_snapshot(maturity_days: int) -> dict[str, object]:
    return {
        "algorithm": "packaging-guarded-rules-v1",
        "maturity_days": maturity_days,
        "minimum_impressions_per_variant": _MIN_IMPRESSIONS,
        "minimum_ctr_relative_uplift": _MIN_CTR_RELATIVE_UPLIFT,
        "maximum_average_view_percentage_drop": _MAX_AVG_VIEW_PCT_DROP,
        "maximum_retention_50_drop": _MAX_RETENTION_50_DROP,
        "minimum_chronological_comparisons": _MIN_CHRONOLOGICAL_COMPARISONS,
        "automatic_public_mutation": False,
        "mixed_reach_days_excluded": True,
        "missing_values_coerced_to_zero": False,
    }


def refresh_packaging_intelligence(
    channel_profile_id: uuid.UUID,
    *,
    run_key: str,
    maturity_days: int = _DEFAULT_MATURITY_DAYS,
) -> PackagingIntelligenceSnapshot:
    key = run_key.strip()
    if not key:
        raise ValueError("packaging intelligence run_key is required")
    if len(key) > 160:
        raise ValueError("packaging intelligence run_key must be 160 characters or fewer")
    if maturity_days not in _ALLOWED_MATURITY_DAYS:
        raise ValueError(
            "packaging maturity days must be one of "
            + ", ".join(str(value) for value in _ALLOWED_MATURITY_DAYS)
        )

    with session_scope() as session:
        profile = session.get(ChannelProfile, channel_profile_id)
        if profile is None:
            raise ValueError(f"channel profile not found: {channel_profile_id}")
        existing = session.scalar(
            select(PackagingIntelligenceSnapshot).where(
                PackagingIntelligenceSnapshot.channel_profile_id
                == channel_profile_id,
                PackagingIntelligenceSnapshot.run_key == key,
            )
        )
        if existing is not None:
            if existing.maturity_days != maturity_days:
                raise ValueError("run_key is already bound to another maturity window")
            return existing

        connection = session.get(YouTubeConnection, profile.youtube_connection_id)
        if connection is None:
            raise ValueError("channel profile references a missing YouTube connection")
        publications = list(
            session.scalars(
                select(Publication)
                .where(
                    Publication.youtube_connection_id
                    == profile.youtube_connection_id,
                    Publication.youtube_video_id.is_not(None),
                )
                .order_by(Publication.created_at)
            )
        )
        publication_ids = [row.id for row in publications]
        variants = list(
            session.scalars(
                select(PublicationPackagingVariant)
                .where(
                    PublicationPackagingVariant.publication_id.in_(
                        publication_ids
                    )
                )
                .order_by(
                    PublicationPackagingVariant.publication_id,
                    PublicationPackagingVariant.created_at,
                )
            )
        ) if publication_ids else []
        variants_by_publication: dict[
            uuid.UUID,
            list[PublicationPackagingVariant],
        ] = defaultdict(list)
        variant_by_id = {variant.id: variant for variant in variants}
        for variant in variants:
            variants_by_publication[variant.publication_id].append(variant)

        reach_rows = list(
            session.scalars(
                select(PublicationReachObservation)
                .where(
                    PublicationReachObservation.publication_id.in_(
                        publication_ids
                    )
                )
                .order_by(
                    PublicationReachObservation.publication_id,
                    PublicationReachObservation.report_date,
                )
            )
        ) if publication_ids else []
        reach_by_publication: dict[
            uuid.UUID,
            list[PublicationReachObservation],
        ] = defaultdict(list)
        for row in reach_rows:
            reach_by_publication[row.publication_id].append(row)

        measured_rows: list[PackagingVariantPerformanceWindow] = []
        publication_by_id = {row.id: row for row in publications}
        for publication_id, publication_reach in reach_by_publication.items():
            publication = publication_by_id[publication_id]
            for window in _eligible_reach_windows(
                publication_reach,
                maturity_days,
            ):
                variant_id = window[0].packaging_variant_id
                variant = variant_by_id.get(variant_id) if variant_id else None
                if variant is None:
                    continue
                measured = _measure_window(
                    session,
                    publication=publication,
                    variant=variant,
                    rows=window,
                    maturity_days=maturity_days,
                    connection=connection,
                )
                if measured is not None:
                    measured_rows.append(measured)

        measured_payload = [
            _window_payload(row, variant_by_id[row.packaging_variant_id])
            for row in measured_rows
        ]
        recommendations = _recommendations(
            publications,
            variants_by_publication,
            measured_payload,
        )
        validation = _chronological_validation(recommendations)
        recommendation_count = sum(
            row.get("recommendation_type") in {"prefer", "test"}
            for row in recommendations
        )
        if recommendation_count:
            recommendation_status = "advisory_recommendations"
        elif measured_payload:
            recommendation_status = "evidence_available"
        else:
            recommendation_status = "insufficient_data"

        version = int(
            session.scalar(
                select(
                    func.coalesce(
                        func.max(PackagingIntelligenceSnapshot.version),
                        0,
                    )
                ).where(
                    PackagingIntelligenceSnapshot.channel_profile_id
                    == channel_profile_id
                )
            )
            or 0
        ) + 1
        sample_dates = [
            date.fromisoformat(str(row["window_start"]))
            for row in measured_payload
        ] + [
            date.fromisoformat(str(row["window_end"]))
            for row in measured_payload
        ]
        snapshot = PackagingIntelligenceSnapshot(
            channel_profile_id=channel_profile_id,
            version=version,
            run_key=key,
            maturity_days=maturity_days,
            publication_count=len(publications),
            variant_window_count=len(measured_payload),
            recommendation_count=recommendation_count,
            recommendation_status=recommendation_status,
            variant_metrics=measured_payload,
            recommendations=recommendations,
            validation_metrics=validation,
            policy_snapshot=_policy_snapshot(maturity_days),
            sample_window_start=min(sample_dates) if sample_dates else None,
            sample_window_end=max(sample_dates) if sample_dates else None,
        )
        session.add(snapshot)
        session.flush()
        session.add(
            DomainEvent(
                aggregate_type="channel_profile",
                aggregate_id=str(channel_profile_id),
                event_type="channel_profile.packaging_intelligence_refreshed",
                payload={
                    "channel_profile_id": str(channel_profile_id),
                    "snapshot_id": str(snapshot.id),
                    "version": version,
                    "maturity_days": maturity_days,
                    "variant_window_count": len(measured_payload),
                    "recommendation_count": recommendation_count,
                    "recommendation_status": recommendation_status,
                    "automatic_public_mutation": False,
                },
            )
        )
        session.refresh(snapshot)
        session.expunge(snapshot)
        return snapshot


def latest_packaging_intelligence(
    channel_profile_id: uuid.UUID,
    *,
    maturity_days: int | None = None,
) -> PackagingIntelligenceSnapshot | None:
    with session_scope() as session:
        stmt = (
            select(PackagingIntelligenceSnapshot)
            .where(
                PackagingIntelligenceSnapshot.channel_profile_id
                == channel_profile_id
            )
            .order_by(PackagingIntelligenceSnapshot.version.desc())
            .limit(1)
        )
        if maturity_days is not None:
            stmt = stmt.where(
                PackagingIntelligenceSnapshot.maturity_days == maturity_days
            )
        row = session.scalar(stmt)
        if row is not None:
            session.expunge(row)
        return row


def list_packaging_intelligence(
    channel_profile_id: uuid.UUID,
    *,
    maturity_days: int | None = None,
    limit: int = 50,
) -> list[PackagingIntelligenceSnapshot]:
    with session_scope() as session:
        stmt = (
            select(PackagingIntelligenceSnapshot)
            .where(
                PackagingIntelligenceSnapshot.channel_profile_id
                == channel_profile_id
            )
            .order_by(PackagingIntelligenceSnapshot.version.desc())
            .limit(limit)
        )
        if maturity_days is not None:
            stmt = stmt.where(
                PackagingIntelligenceSnapshot.maturity_days == maturity_days
            )
        rows = list(session.scalars(stmt))
        for row in rows:
            session.expunge(row)
        return rows
