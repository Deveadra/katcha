from __future__ import annotations

import uuid
from collections import defaultdict
from datetime import datetime
from decimal import Decimal
from itertools import combinations
from typing import Any

from sqlalchemy import func, select

from katcha.db import session_scope
from katcha.edit_performance_models import EditBlueprintPerformanceSnapshot
from katcha.intelligence_models import ChannelProfile, PerformanceObservation
from katcha.models import DomainEvent
from katcha.production_models import Production
from katcha.publishing_models import (
    Publication,
    PublicationAnalyticsSnapshot,
    RetentionPoint,
)
from katcha.services.channel_learning import derive_performance_observations
from katcha.short_episode_models import ShortEpisode

_MIN_GROUP_SAMPLE = 5
_MIN_MARGIN_SAMPLE = 3
_MIN_MONETARY_COVERAGE = 0.60
_MIN_RETENTION_COVERAGE = 0.50
_AGE_BUCKETS = (6, 24, 72, 168)
_DEFAULT_AGE_BUCKET = 72
_RETENTION_TARGETS = (0.25, 0.50, 0.75, 0.95)
_RETENTION_MAX_DISTANCE = 0.08


def _decimal(value: float | Decimal | int) -> Decimal:
    return Decimal(str(round(float(value), 8)))


def _mean(values: list[float]) -> float | None:
    return round(sum(values) / len(values), 6) if values else None


def _number(value: object) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _source_lineage(
    session: object,
    publication: Publication,
) -> tuple[str, uuid.UUID, dict[str, object], Decimal] | None:
    metadata = dict(publication.treatment_metadata or {})
    if publication.production_id is not None:
        source = session.get(Production, publication.production_id)
        if source is None:
            return None
        source_kind = "production"
        source_id = source.id
        fallback_scope = source.kind
        fallback_snapshot = dict(source.edit_blueprint_snapshot or {})
        cost = _production_lineage_cost(session, source)
        fallback = {
            "edit_blueprint_key": source.edit_blueprint_key,
            "edit_blueprint_revision": source.edit_blueprint_version,
            "edit_blueprint_contract_version": fallback_snapshot.get("version"),
            "edit_composition": fallback_snapshot.get("composition"),
            "edit_narration_mode": dict(
                fallback_snapshot.get("narration") or {}
            ).get("mode"),
            "edit_source_layout_mode": dict(
                fallback_snapshot.get("source_layout") or {}
            ).get("mode"),
            "source_scope": fallback_scope,
            "selected_style": None,
            "brand_key": source.brand_key,
            "brand_version": source.brand_version,
        }
    elif publication.short_episode_id is not None:
        source = session.get(ShortEpisode, publication.short_episode_id)
        if source is None:
            return None
        source_kind = "short_episode"
        source_id = source.id
        fallback_scope = source.format_key
        fallback_snapshot = dict(source.edit_blueprint_snapshot or {})
        cost = _episode_lineage_cost(session, source)
        fallback = {
            "edit_blueprint_key": source.edit_blueprint_key,
            "edit_blueprint_revision": source.edit_blueprint_version,
            "edit_blueprint_contract_version": fallback_snapshot.get("version"),
            "edit_composition": fallback_snapshot.get("composition"),
            "edit_narration_mode": dict(
                fallback_snapshot.get("narration") or {}
            ).get("mode"),
            "edit_source_layout_mode": dict(
                fallback_snapshot.get("source_layout") or {}
            ).get("mode"),
            "source_scope": fallback_scope,
            "selected_style": None,
            "brand_key": source.brand_key,
            "brand_version": source.brand_version,
        }
    else:
        return None

    lineage = {**fallback, **{key: value for key, value in metadata.items() if value is not None}}
    blueprint_key = str(lineage.get("edit_blueprint_key") or "").strip()
    if not blueprint_key:
        return None
    revision_raw = lineage.get("edit_blueprint_revision")
    try:
        revision = int(revision_raw) if revision_raw is not None else None
    except (TypeError, ValueError):
        revision = None
    lineage["edit_blueprint_revision"] = revision
    lineage["source_kind"] = source_kind
    lineage["source_scope"] = str(lineage.get("source_scope") or fallback_scope)
    lineage["lineage_source"] = (
        "publication_metadata"
        if metadata.get("edit_blueprint_key")
        else "frozen_source_fallback"
    )
    return source_kind, source_id, lineage, cost


def _production_lineage_cost(session: object, source: Production) -> Decimal:
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


def _episode_lineage_cost(session: object, source: ShortEpisode) -> Decimal:
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


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _publication_anchor(publication: Publication) -> datetime:
    return _utc(
        publication.published_at
        or publication.publish_at
        or publication.created_at
    )


def _publication_age_hours(
    publication: Publication,
    snapshot: PublicationAnalyticsSnapshot,
) -> float:
    return max(
        0.0,
        (_utc(snapshot.sampled_at) - _publication_anchor(publication)).total_seconds()
        / 3600.0,
    )


def _analytics_at_age_bucket(
    session: object,
    publications: list[Publication],
    age_bucket_hours: int,
) -> dict[uuid.UUID, PublicationAnalyticsSnapshot]:
    publication_ids = [row.id for row in publications]
    if not publication_ids:
        return {}
    rows = list(
        session.scalars(
            select(PublicationAnalyticsSnapshot)
            .where(PublicationAnalyticsSnapshot.publication_id.in_(publication_ids))
            .order_by(
                PublicationAnalyticsSnapshot.publication_id,
                PublicationAnalyticsSnapshot.sampled_at,
            )
        )
    )
    by_publication: dict[uuid.UUID, list[PublicationAnalyticsSnapshot]] = defaultdict(list)
    for row in rows:
        by_publication[row.publication_id].append(row)

    publication_by_id = {row.id: row for row in publications}
    tolerance = max(2.0, age_bucket_hours * 0.35)
    selected: dict[uuid.UUID, PublicationAnalyticsSnapshot] = {}
    for publication_id, snapshots in by_publication.items():
        publication = publication_by_id[publication_id]
        eligible = [
            row
            for row in snapshots
            if abs(_publication_age_hours(publication, row) - age_bucket_hours)
            <= tolerance
        ]
        if not eligible:
            continue
        selected[publication_id] = min(
            eligible,
            key=lambda row: abs(
                _publication_age_hours(publication, row) - age_bucket_hours
            ),
        )
    return selected


def _retention_by_snapshot(
    session: object,
    snapshot_ids: list[uuid.UUID],
) -> dict[uuid.UUID, list[RetentionPoint]]:
    if not snapshot_ids:
        return {}
    grouped: dict[uuid.UUID, list[RetentionPoint]] = defaultdict(list)
    for row in session.scalars(
        select(RetentionPoint)
        .where(RetentionPoint.snapshot_id.in_(snapshot_ids))
        .order_by(RetentionPoint.snapshot_id, RetentionPoint.elapsed_video_time_ratio)
    ):
        grouped[row.snapshot_id].append(row)
    return grouped


def _retention_value(points: list[RetentionPoint], target: float) -> float | None:
    candidates = [
        point for point in points if point.audience_watch_ratio is not None
    ]
    if not candidates:
        return None
    nearest = min(
        candidates,
        key=lambda point: abs(float(point.elapsed_video_time_ratio) - target),
    )
    if abs(float(nearest.elapsed_video_time_ratio) - target) > _RETENTION_MAX_DISTANCE:
        return None
    return float(nearest.audience_watch_ratio)


def _group_identity(lineage: dict[str, object]) -> dict[str, object]:
    key = str(lineage.get("edit_blueprint_key") or "")
    revision = lineage.get("edit_blueprint_revision")
    contract = str(lineage.get("edit_blueprint_contract_version") or "")
    narration = str(lineage.get("edit_narration_mode") or "")
    layout = str(lineage.get("edit_source_layout_mode") or "")
    style = str(lineage.get("selected_style") or "")
    source_kind = str(lineage.get("source_kind") or "")
    scope = str(lineage.get("source_scope") or "")
    treatment_key = "|".join(
        [
            source_kind,
            scope,
            f"{key}@r{revision or 'legacy'}",
            contract or "unknown",
            narration or "unknown",
            layout or "unknown",
            style or "default",
        ]
    )
    return {
        "group_key": treatment_key,
        "source_kind": source_kind,
        "source_scope": scope,
        "edit_blueprint_key": key,
        "edit_blueprint_revision": revision,
        "edit_blueprint_contract_version": contract or None,
        "edit_composition": lineage.get("edit_composition"),
        "edit_narration_mode": narration or None,
        "edit_source_layout_mode": layout or None,
        "selected_style": style or None,
        "brand_key": lineage.get("brand_key"),
        "brand_version": lineage.get("brand_version"),
    }


def _aggregate_group(
    identity: dict[str, object],
    rows: list[dict[str, object]],
) -> dict[str, object]:
    def numbers(key: str) -> list[float]:
        return [
            value
            for row in rows
            if (value := _number(row.get(key))) is not None
        ]

    revenue_rows = [row for row in rows if row.get("estimated_revenue") is not None]
    known_revenue = sum(
        (Decimal(str(row["estimated_revenue"])) for row in revenue_rows),
        Decimal("0"),
    )
    total_cost = sum(
        (Decimal(str(row["attributed_cost_usd"])) for row in rows),
        Decimal("0"),
    )
    covered_cost = sum(
        (Decimal(str(row["attributed_cost_usd"])) for row in revenue_rows),
        Decimal("0"),
    )
    covered_margin = known_revenue - covered_cost if revenue_rows else None
    count = len(rows)
    retention = {
        f"mean_audience_watch_ratio_{int(target * 100)}pct": _mean(
            numbers(f"retention_{int(target * 100)}")
        )
        for target in _RETENTION_TARGETS
    }
    confidence = round(count / (count + 5.0), 6) if count else 0.0

    return {
        **identity,
        "publication_count": count,
        "confidence": confidence,
        "mean_outcome_score": _mean(numbers("outcome_score")),
        "mean_views": _mean(numbers("views")),
        "mean_engaged_views": _mean(numbers("engaged_views")),
        "mean_average_view_duration_seconds": _mean(
            numbers("average_view_duration")
        ),
        "mean_average_view_percentage": _mean(
            numbers("average_view_percentage")
        ),
        "mean_likes": _mean(numbers("likes")),
        "mean_comments": _mean(numbers("comments")),
        "mean_shares": _mean(numbers("shares")),
        "mean_subscribers_gained": _mean(numbers("subscribers_gained")),
        "mean_subscribers_lost": _mean(numbers("subscribers_lost")),
        **retention,
        "attributed_cost_usd": str(total_cost),
        "revenue_covered_publications": len(revenue_rows),
        "monetary_coverage": round(len(revenue_rows) / count, 6) if count else 0.0,
        "covered_revenue_usd": str(known_revenue) if revenue_rows else None,
        "covered_cost_usd": str(covered_cost) if revenue_rows else None,
        "covered_contribution_margin_usd": (
            str(covered_margin) if covered_margin is not None else None
        ),
        "covered_margin_per_publication_usd": (
            str(covered_margin / Decimal(len(revenue_rows)))
            if covered_margin is not None and revenue_rows
            else None
        ),
        "sample_start": min(
            (str(row["sampled_at"]) for row in rows if row.get("sampled_at")),
            default=None,
        ),
        "sample_end": max(
            (str(row["sampled_at"]) for row in rows if row.get("sampled_at")),
            default=None,
        ),
    }


def _comparison_summary(groups: list[dict[str, object]]) -> tuple[str, dict[str, Any]]:
    comparisons: list[dict[str, object]] = []
    eligible = [
        group
        for group in groups
        if int(group.get("publication_count") or 0) >= _MIN_GROUP_SAMPLE
    ]
    for left, right in combinations(eligible, 2):
        if (
            left.get("source_kind") != right.get("source_kind")
            or left.get("source_scope") != right.get("source_scope")
        ):
            continue
        left_view = _number(left.get("mean_average_view_percentage"))
        right_view = _number(right.get("mean_average_view_percentage"))
        left_outcome = _number(left.get("mean_outcome_score"))
        right_outcome = _number(right.get("mean_outcome_score"))
        delta: dict[str, object] = {}
        if left_view is not None and right_view is not None:
            delta["average_view_percentage"] = round(left_view - right_view, 6)
        if left_outcome is not None and right_outcome is not None:
            delta["outcome_score"] = round(left_outcome - right_outcome, 6)

        left_revenue_n = int(left.get("revenue_covered_publications") or 0)
        right_revenue_n = int(right.get("revenue_covered_publications") or 0)
        if left_revenue_n >= _MIN_MARGIN_SAMPLE and right_revenue_n >= _MIN_MARGIN_SAMPLE:
            left_margin = _number(left.get("covered_margin_per_publication_usd"))
            right_margin = _number(right.get("covered_margin_per_publication_usd"))
            if left_margin is not None and right_margin is not None:
                delta["covered_margin_per_publication_usd"] = round(
                    left_margin - right_margin,
                    6,
                )
        comparisons.append(
            {
                "source_kind": left.get("source_kind"),
                "source_scope": left.get("source_scope"),
                "left_group_key": left.get("group_key"),
                "right_group_key": right.get("group_key"),
                "left_sample_count": left.get("publication_count"),
                "right_sample_count": right.get("publication_count"),
                "confidence": min(
                    float(left.get("confidence") or 0),
                    float(right.get("confidence") or 0),
                ),
                "observed_delta_left_minus_right": delta,
            }
        )

    status = "evidence_available" if comparisons else "insufficient_data"
    return status, {
        "advisory_only": True,
        "minimum_group_sample": _MIN_GROUP_SAMPLE,
        "minimum_margin_sample": _MIN_MARGIN_SAMPLE,
        "eligible_group_count": len(eligible),
        "comparisons": comparisons,
    }


def refresh_edit_blueprint_performance(
    channel_profile_id: uuid.UUID,
    *,
    run_key: str,
    age_bucket_hours: int = _DEFAULT_AGE_BUCKET,
) -> EditBlueprintPerformanceSnapshot:
    key = run_key.strip()
    if not key:
        raise ValueError("edit performance run_key is required")
    if len(key) > 160:
        raise ValueError("edit performance run_key must be 160 characters or fewer")
    if age_bucket_hours not in _AGE_BUCKETS:
        raise ValueError(
            "edit performance age bucket must be one of "
            + ", ".join(str(value) for value in _AGE_BUCKETS)
        )

    derive_performance_observations(channel_profile_id)

    with session_scope() as session:
        profile = session.get(ChannelProfile, channel_profile_id)
        if profile is None:
            raise ValueError(f"channel profile not found: {channel_profile_id}")
        existing = session.scalar(
            select(EditBlueprintPerformanceSnapshot).where(
                EditBlueprintPerformanceSnapshot.channel_profile_id == channel_profile_id,
                EditBlueprintPerformanceSnapshot.run_key == key,
            )
        )
        if existing is not None:
            if existing.age_bucket_hours != age_bucket_hours:
                raise ValueError("run_key is already bound to another age bucket")
            return existing

        publications = list(
            session.scalars(
                select(Publication).where(
                    Publication.youtube_connection_id == profile.youtube_connection_id
                )
            )
        )
        selected_analytics = _analytics_at_age_bucket(
            session,
            publications,
            age_bucket_hours,
        )
        selected_snapshot_ids = [row.id for row in selected_analytics.values()]
        observation_by_snapshot = {
            row.analytics_snapshot_id: row
            for row in session.scalars(
                select(PerformanceObservation).where(
                    PerformanceObservation.channel_profile_id == channel_profile_id,
                    PerformanceObservation.analytics_snapshot_id.in_(
                        selected_snapshot_ids
                    ),
                )
            )
        } if selected_snapshot_ids else {}
        retention_by_snapshot = _retention_by_snapshot(
            session,
            selected_snapshot_ids,
        )

        grouped: dict[str, list[dict[str, object]]] = defaultdict(list)
        identity_by_key: dict[str, dict[str, object]] = {}
        sample_times: list[datetime] = []
        revenue_covered = 0
        retention_covered = 0
        missing_lineage = 0

        for publication in publications:
            analytics = selected_analytics.get(publication.id)
            if analytics is None:
                continue
            lineage_result = _source_lineage(session, publication)
            if lineage_result is None:
                missing_lineage += 1
                continue
            _, _, lineage, cost = lineage_result
            identity = _group_identity(lineage)
            group_key = str(identity["group_key"])
            identity_by_key[group_key] = identity
            points = retention_by_snapshot.get(analytics.id, [])
            observation = observation_by_snapshot.get(analytics.id)
            record: dict[str, object] = {
                "publication_id": str(publication.id),
                "sampled_at": analytics.sampled_at.isoformat(),
                "views": analytics.views,
                "engaged_views": analytics.engaged_views,
                "average_view_duration": analytics.average_view_duration,
                "average_view_percentage": analytics.average_view_percentage,
                "likes": analytics.likes,
                "comments": analytics.comments,
                "shares": analytics.shares,
                "subscribers_gained": analytics.subscribers_gained,
                "subscribers_lost": analytics.subscribers_lost,
                "estimated_revenue": analytics.estimated_revenue,
                "outcome_score": (
                    observation.outcome_score if observation is not None else None
                ),
                "attributed_cost_usd": cost,
                "interaction_rate": (
                    (
                        int(analytics.likes or 0)
                        + int(analytics.comments or 0)
                        + int(analytics.shares or 0)
                    )
                    / max(int(analytics.views or analytics.engaged_views or 0), 1)
                ),
                "subscribers_net": (
                    int(analytics.subscribers_gained or 0)
                    - int(analytics.subscribers_lost or 0)
                ),
            }
            retention_values: list[float] = []
            for target in _RETENTION_TARGETS:
                retention_value = _retention_value(points, target)
                record[f"retention_{int(target * 100)}"] = retention_value
                if retention_value is not None:
                    retention_values.append(retention_value)
            record["has_retention"] = bool(retention_values)
            grouped[group_key].append(record)
            sample_times.append(analytics.sampled_at)
            if analytics.estimated_revenue is not None:
                revenue_covered += 1
            if retention_values:
                retention_covered += 1

        aggregates = [
            _aggregate_group(identity_by_key[key_name], rows)
            for key_name, rows in sorted(grouped.items())
        ]
        comparison_status, comparison_summary = _comparison_summary(aggregates)
        publication_count = sum(len(rows) for rows in grouped.values())
        monetary_coverage = (
            revenue_covered / publication_count if publication_count else 0.0
        )
        retention_coverage = (
            retention_covered / publication_count if publication_count else 0.0
        )
        comparison_summary.update(
            {
                "age_bucket_hours": age_bucket_hours,
                "channel_publication_count": len(publications),
                "maturity_matched_publications": len(selected_analytics),
                "lineage_covered_publications": publication_count,
                "excluded_missing_lineage": missing_lineage,
                "monetary_coverage": round(monetary_coverage, 6),
                "retention_coverage": round(retention_coverage, 6),
            }
        )
        version = int(
            session.scalar(
                select(
                    func.coalesce(
                        func.max(EditBlueprintPerformanceSnapshot.version),
                        0,
                    )
                ).where(
                    EditBlueprintPerformanceSnapshot.channel_profile_id
                    == channel_profile_id
                )
            )
            or 0
        ) + 1
        snapshot = EditBlueprintPerformanceSnapshot(
            channel_profile_id=channel_profile_id,
            version=version,
            run_key=key,
            age_bucket_hours=age_bucket_hours,
            publication_count=publication_count,
            blueprint_group_count=len(aggregates),
            revenue_covered_publications=revenue_covered,
            retention_covered_publications=retention_covered,
            monetary_coverage=_decimal(monetary_coverage),
            retention_coverage=_decimal(retention_coverage),
            aggregate_metrics=aggregates,
            comparison_status=comparison_status,
            comparison_summary=comparison_summary,
            sample_window_start=min(sample_times, default=None),
            sample_window_end=max(sample_times, default=None),
        )
        session.add(snapshot)
        session.flush()
        session.add(
            DomainEvent(
                aggregate_type="channel_profile",
                aggregate_id=str(channel_profile_id),
                event_type="channel_profile.edit_blueprint_performance_refreshed",
                payload={
                    "channel_profile_id": str(channel_profile_id),
                    "snapshot_id": str(snapshot.id),
                    "version": version,
                    "run_key": key,
                    "age_bucket_hours": age_bucket_hours,
                    "publication_count": publication_count,
                    "blueprint_group_count": len(aggregates),
                    "monetary_coverage": round(monetary_coverage, 6),
                    "retention_coverage": round(retention_coverage, 6),
                    "group_keys": [
                        str(group.get("group_key") or "") for group in aggregates
                    ],
                    "comparison_status": comparison_status,
                },
            )
        )
        session.refresh(snapshot)
        session.expunge(snapshot)
        return snapshot


def latest_edit_blueprint_performance(
    channel_profile_id: uuid.UUID,
    *,
    age_bucket_hours: int | None = None,
) -> EditBlueprintPerformanceSnapshot | None:
    with session_scope() as session:
        query = select(EditBlueprintPerformanceSnapshot).where(
            EditBlueprintPerformanceSnapshot.channel_profile_id
            == channel_profile_id
        )
        if age_bucket_hours is not None:
            query = query.where(
                EditBlueprintPerformanceSnapshot.age_bucket_hours
                == age_bucket_hours
            )
        return session.scalar(
            query.order_by(EditBlueprintPerformanceSnapshot.version.desc()).limit(1)
        )


def list_edit_blueprint_performance(
    channel_profile_id: uuid.UUID,
    *,
    limit: int = 50,
    age_bucket_hours: int | None = None,
) -> list[EditBlueprintPerformanceSnapshot]:
    with session_scope() as session:
        query = select(EditBlueprintPerformanceSnapshot).where(
            EditBlueprintPerformanceSnapshot.channel_profile_id
            == channel_profile_id
        )
        if age_bucket_hours is not None:
            query = query.where(
                EditBlueprintPerformanceSnapshot.age_bucket_hours
                == age_bucket_hours
            )
        return list(
            session.scalars(
                query.order_by(EditBlueprintPerformanceSnapshot.version.desc())
                .limit(max(1, min(limit, 250)))
            )
        )
