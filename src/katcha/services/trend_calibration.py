from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from statistics import median
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from katcha.db import session_scope
from katcha.intelligence.learning import outcome_score
from katcha.intelligence_models import ChannelProfile
from katcha.models import DomainEvent
from katcha.publishing_models import Publication, PublicationAnalyticsSnapshot
from katcha.short_episode_models import ShortEpisode
from katcha.trend_calibration_models import (
    TrendCalibrationSnapshot,
    TrendOpportunityOutcome,
    TrendOutcomeAttribution,
)
from katcha.trend_models import TrendOpportunity
from katcha.trends.calibration import (
    CALIBRATION_FEATURES,
    CalibrationResult,
    CalibrationRow,
    blended_score,
    clamp,
    opportunity_features,
    train_calibration,
)

AGE_BUCKETS = (6, 24, 72, 168)
MIN_BASELINE_SAMPLES = 3
BASELINE_HISTORY_LIMIT = 20
BREAKOUT_LIFT_THRESHOLD = 1.25


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _decimal(value: float | int | Decimal) -> Decimal:
    return Decimal(str(value))


def _publication_anchor(publication: Publication) -> datetime:
    return _utc(
        publication.published_at
        or publication.publish_at
        or publication.created_at
    )


def _age_hours(
    publication: Publication,
    snapshot: PublicationAnalyticsSnapshot,
) -> float:
    return max(
        0.0,
        (_utc(snapshot.sampled_at) - _publication_anchor(publication)).total_seconds()
        / 3600.0,
    )


def age_bucket(age_hours: float) -> int | None:
    candidates: list[tuple[float, int]] = []
    for bucket in AGE_BUCKETS:
        tolerance = max(2.0, bucket * 0.35)
        distance = abs(age_hours - bucket)
        if distance <= tolerance:
            candidates.append((distance, bucket))
    return min(candidates)[1] if candidates else None


def _snapshot_outcome(
    publication: Publication,
    snapshot: PublicationAnalyticsSnapshot,
) -> tuple[float, dict[str, Any]]:
    age = max(1.0, _age_hours(publication, snapshot))
    views = int(snapshot.views or 0)
    engaged_views = int(snapshot.engaged_views or 0)
    score, signals = outcome_score(
        views=views,
        engaged_views=engaged_views,
        age_hours=age,
        average_view_percentage=float(snapshot.average_view_percentage or 0),
        shares=int(snapshot.shares or 0),
        comments=int(snapshot.comments or 0),
        subscribers_gained=int(snapshot.subscribers_gained or 0),
    )
    audience = max(views, engaged_views, 1)
    interactions = (
        int(snapshot.likes or 0)
        + int(snapshot.comments or 0)
        + int(snapshot.shares or 0)
    )
    return score, {
        **signals,
        "views": views,
        "engaged_views": engaged_views,
        "estimated_minutes_watched": (
            float(snapshot.estimated_minutes_watched)
            if snapshot.estimated_minutes_watched is not None
            else None
        ),
        "average_view_duration": (
            float(snapshot.average_view_duration)
            if snapshot.average_view_duration is not None
            else None
        ),
        "average_view_percentage": float(snapshot.average_view_percentage or 0),
        "likes": int(snapshot.likes or 0),
        "comments": int(snapshot.comments or 0),
        "shares": int(snapshot.shares or 0),
        "interaction_rate": round(interactions / audience, 8),
        "subscribers_gained": int(snapshot.subscribers_gained or 0),
        "subscribers_lost": int(snapshot.subscribers_lost or 0),
        "estimated_revenue": (
            float(snapshot.estimated_revenue)
            if snapshot.estimated_revenue is not None
            else None
        ),
        "monetized_playbacks": (
            int(snapshot.monetized_playbacks)
            if snapshot.monetized_playbacks is not None
            else None
        ),
    }


def _closest_bucket_snapshot(
    publication: Publication,
    snapshots: list[PublicationAnalyticsSnapshot],
    bucket: int,
) -> PublicationAnalyticsSnapshot | None:
    eligible = [
        item for item in snapshots if age_bucket(_age_hours(publication, item)) == bucket
    ]
    if not eligible:
        return None
    return min(
        eligible,
        key=lambda item: abs(_age_hours(publication, item) - bucket),
    )


def _historical_baseline(
    session: Session,
    profile: ChannelProfile,
    publication: Publication,
    snapshot: PublicationAnalyticsSnapshot,
    bucket: int,
) -> tuple[int, float | None]:
    current_anchor = _publication_anchor(publication)
    prior_publications = list(
        session.scalars(
            select(Publication)
            .where(
                Publication.youtube_connection_id == profile.youtube_connection_id,
                Publication.id != publication.id,
                Publication.created_at < current_anchor,
            )
            .order_by(Publication.created_at.desc())
            .limit(BASELINE_HISTORY_LIMIT * 2)
        )
    )
    scores: list[float] = []
    for prior in prior_publications:
        if _publication_anchor(prior) >= current_anchor:
            continue
        snapshots = list(
            session.scalars(
                select(PublicationAnalyticsSnapshot)
                .where(
                    PublicationAnalyticsSnapshot.publication_id == prior.id,
                    PublicationAnalyticsSnapshot.sampled_at < snapshot.sampled_at,
                )
                .order_by(PublicationAnalyticsSnapshot.sampled_at)
            )
        )
        comparable = _closest_bucket_snapshot(prior, snapshots, bucket)
        if comparable is None:
            continue
        score, _ = _snapshot_outcome(prior, comparable)
        scores.append(score)
        if len(scores) >= BASELINE_HISTORY_LIMIT:
            break
    if not scores:
        return 0, None
    return len(scores), float(median(scores))


def _trend_lineage(
    session: Session,
    profile: ChannelProfile,
    publication: Publication,
) -> tuple[TrendOpportunity | None, str, str]:
    if publication.short_episode_id is None:
        return None, "unattributed", "publication_not_created_from_short_episode"
    episode = session.get(ShortEpisode, publication.short_episode_id)
    if episode is None:
        return None, "invalid_lineage", "short_episode_missing"
    if episode.channel_profile_id != profile.id:
        return None, "invalid_lineage", "short_episode_channel_mismatch"
    if episode.trend_opportunity_id is None:
        return None, "unattributed", "short_episode_has_no_trend_opportunity"
    opportunity = session.get(TrendOpportunity, episode.trend_opportunity_id)
    if opportunity is None:
        return None, "invalid_lineage", "trend_opportunity_missing"
    if opportunity.channel_profile_id != profile.id:
        return None, "invalid_lineage", "trend_opportunity_channel_mismatch"
    metadata_id = (publication.treatment_metadata or {}).get("trend_opportunity_id")
    if metadata_id and str(metadata_id) != str(opportunity.id):
        return None, "invalid_lineage", "publication_treatment_lineage_mismatch"
    if _publication_anchor(publication) < _utc(opportunity.created_at):
        return None, "invalid_lineage", "publication_precedes_prediction"
    return opportunity, "attributed", "stable_short_episode_trend_fk"


def derive_trend_outcomes(channel_profile_id: uuid.UUID) -> dict[str, int]:
    created_attributions = 0
    created_outcomes = 0
    with session_scope() as session:
        profile = session.get(ChannelProfile, channel_profile_id)
        if profile is None:
            raise ValueError(f"channel profile not found: {channel_profile_id}")
        publications = list(
            session.scalars(
                select(Publication).where(
                    Publication.youtube_connection_id == profile.youtube_connection_id
                )
            )
        )
        publication_by_id = {item.id: item for item in publications}
        if not publication_by_id:
            return {"attributions": 0, "outcomes": 0}
        snapshots = list(
            session.scalars(
                select(PublicationAnalyticsSnapshot)
                .where(
                    PublicationAnalyticsSnapshot.publication_id.in_(
                        list(publication_by_id)
                    )
                )
                .order_by(PublicationAnalyticsSnapshot.sampled_at)
            )
        )
        existing_attributions = set(
            session.scalars(
                select(TrendOutcomeAttribution.analytics_snapshot_id).where(
                    TrendOutcomeAttribution.analytics_snapshot_id.in_(
                        [item.id for item in snapshots]
                    )
                )
            )
        )
        selected_buckets: dict[uuid.UUID, int] = {}
        snapshots_by_publication: dict[
            uuid.UUID, list[PublicationAnalyticsSnapshot]
        ] = {}
        for item in snapshots:
            snapshots_by_publication.setdefault(item.publication_id, []).append(item)
        for publication_id, publication_snapshots in snapshots_by_publication.items():
            publication = publication_by_id[publication_id]
            for bucket_value in AGE_BUCKETS:
                selected = _closest_bucket_snapshot(
                    publication,
                    publication_snapshots,
                    bucket_value,
                )
                if selected is not None:
                    selected_buckets[selected.id] = bucket_value

        for snapshot in snapshots:
            if snapshot.id in existing_attributions:
                continue
            publication = publication_by_id[snapshot.publication_id]
            opportunity, status, reason = _trend_lineage(session, profile, publication)
            attribution = TrendOutcomeAttribution(
                channel_profile_id=profile.id,
                publication_id=publication.id,
                analytics_snapshot_id=snapshot.id,
                trend_opportunity_id=opportunity.id if opportunity else None,
                status=status,
                reason=reason,
                source_kind=(
                    "short_episode"
                    if publication.short_episode_id is not None
                    else "non_trend_publication"
                ),
                attribution_metadata={
                    "short_episode_id": (
                        str(publication.short_episode_id)
                        if publication.short_episode_id
                        else None
                    ),
                    "sample_key": snapshot.sample_key,
                },
            )
            session.add(attribution)
            created_attributions += 1
            if opportunity is None:
                continue
            bucket = selected_buckets.get(snapshot.id)
            if bucket is None:
                continue
            observed_score, metrics = _snapshot_outcome(publication, snapshot)
            baseline_count, baseline = _historical_baseline(
                session,
                profile,
                publication,
                snapshot,
                bucket,
            )
            lift = (
                observed_score / baseline
                if baseline is not None and baseline > 1e-9
                else None
            )
            breakout = (
                bool(
                    lift is not None
                    and baseline_count >= MIN_BASELINE_SAMPLES
                    and lift >= BREAKOUT_LIFT_THRESHOLD
                    and observed_score >= min(1.0, baseline + 0.05)
                )
                if baseline is not None and baseline_count >= MIN_BASELINE_SAMPLES
                else None
            )
            anchor = _publication_anchor(publication)
            session.add(
                TrendOpportunityOutcome(
                    channel_profile_id=profile.id,
                    trend_opportunity_id=opportunity.id,
                    publication_id=publication.id,
                    analytics_snapshot_id=snapshot.id,
                    age_bucket_hours=bucket,
                    opportunity_created_at=_utc(opportunity.created_at),
                    published_at=anchor,
                    sampled_at=_utc(snapshot.sampled_at),
                    lead_time_hours=_decimal(
                        round(
                            (anchor - _utc(opportunity.created_at)).total_seconds()
                            / 3600.0,
                            4,
                        )
                    ),
                    predicted_score=opportunity.opportunity_score,
                    predicted_confidence=opportunity.confidence,
                    predicted_lifecycle=opportunity.lifecycle,
                    prediction_components=dict(opportunity.components or {}),
                    observed_outcome_score=_decimal(observed_score),
                    baseline_sample_count=baseline_count,
                    baseline_outcome_score=(
                        _decimal(round(baseline, 6))
                        if baseline is not None
                        else None
                    ),
                    lift_ratio=(
                        _decimal(round(lift, 6)) if lift is not None else None
                    ),
                    realized_breakout=breakout,
                    outcome_metrics={
                        **metrics,
                        "sample_key": snapshot.sample_key,
                        "publication_age_hours": round(
                            _age_hours(publication, snapshot),
                            4,
                        ),
                    },
                )
            )
            created_outcomes += 1
        if created_attributions:
            session.add(
                DomainEvent(
                    aggregate_type="channel_profile",
                    aggregate_id=str(profile.id),
                    event_type="trend.outcomes.attributed",
                    payload={
                        "channel_profile_id": str(profile.id),
                        "attributions_created": created_attributions,
                        "outcomes_created": created_outcomes,
                    },
                )
            )
    return {
        "attributions": created_attributions,
        "outcomes": created_outcomes,
    }


def _relative_target(outcome: TrendOpportunityOutcome) -> float | None:
    if outcome.lift_ratio is None or outcome.baseline_sample_count < MIN_BASELINE_SAMPLES:
        return None
    lift = float(outcome.lift_ratio)
    return clamp(0.5 + (lift - 1.0) * 0.5)


def _training_rows(
    session: Session,
    channel_profile_id: uuid.UUID,
    *,
    target_age_hours: int,
) -> list[CalibrationRow]:
    outcomes = list(
        session.scalars(
            select(TrendOpportunityOutcome)
            .where(
                TrendOpportunityOutcome.channel_profile_id == channel_profile_id,
                TrendOpportunityOutcome.age_bucket_hours == target_age_hours,
            )
            .order_by(TrendOpportunityOutcome.sampled_at)
        )
    )
    rows: list[CalibrationRow] = []
    for outcome in outcomes:
        target = _relative_target(outcome)
        if target is None:
            continue
        rows.append(
            CalibrationRow(
                observed_at=_utc(outcome.sampled_at),
                features=opportunity_features(
                    score=float(outcome.predicted_score),
                    confidence=float(outcome.predicted_confidence),
                    components=dict(outcome.prediction_components or {}),
                ),
                outcome=target,
                realized_breakout=outcome.realized_breakout,
                lead_time_hours=float(outcome.lead_time_hours),
                lifecycle=outcome.predicted_lifecycle,
            )
        )
    return rows


def _result_from_snapshot(snapshot: TrendCalibrationSnapshot) -> CalibrationResult:
    return CalibrationResult(
        algorithm=snapshot.algorithm,
        status=snapshot.status,
        sample_count=snapshot.sample_count,
        training_sample_count=snapshot.training_sample_count,
        validation_sample_count=snapshot.validation_sample_count,
        feature_names=tuple(snapshot.feature_names),
        feature_means={k: float(v) for k, v in snapshot.feature_means.items()},
        feature_scales={k: float(v) for k, v in snapshot.feature_scales.items()},
        coefficients={k: float(v) for k, v in snapshot.coefficients.items()},
        intercept=float(snapshot.intercept),
        confidence=float(snapshot.confidence),
        blend_ratio=float(snapshot.blend_ratio),
        validation_metrics=dict(snapshot.validation_metrics or {}),
        calibration_metrics=dict(snapshot.calibration_metrics or {}),
        training_cutoff=_utc(snapshot.training_cutoff),
    )


def latest_trend_calibration(
    channel_profile_id: uuid.UUID,
) -> TrendCalibrationSnapshot | None:
    with session_scope() as session:
        return session.scalar(
            select(TrendCalibrationSnapshot)
            .where(TrendCalibrationSnapshot.channel_profile_id == channel_profile_id)
            .order_by(TrendCalibrationSnapshot.version.desc())
            .limit(1)
        )


def calibrated_score(
    channel_profile_id: uuid.UUID,
    *,
    score: float,
    confidence: float,
    components: dict[str, float],
) -> tuple[float | None, int | None, dict[str, Any]]:
    snapshot = latest_trend_calibration(channel_profile_id)
    if snapshot is None:
        return None, None, {"status": "untrained"}
    result = _result_from_snapshot(snapshot)
    features = opportunity_features(
        score=score,
        confidence=confidence,
        components=components,
    )
    value, metadata = blended_score(features, result)
    if result.blend_ratio <= 0:
        return None, snapshot.version, {
            **metadata,
            "status": result.status,
            "confidence": result.confidence,
        }
    return value, snapshot.version, {
        **metadata,
        "status": result.status,
        "confidence": result.confidence,
    }


def _window_metrics(
    session: Session,
    channel_profile_id: uuid.UUID,
) -> dict[str, Any]:
    rows = list(
        session.scalars(
            select(TrendOpportunityOutcome)
            .where(
                TrendOpportunityOutcome.channel_profile_id == channel_profile_id
            )
            .order_by(TrendOpportunityOutcome.sampled_at)
        )
    )
    result: dict[str, Any] = {}
    for bucket in AGE_BUCKETS:
        bucket_rows = [row for row in rows if row.age_bucket_hours == bucket]
        labeled = [row for row in bucket_rows if row.realized_breakout is not None]
        breakouts = [row for row in labeled if row.realized_breakout]
        lifts = [float(row.lift_ratio) for row in bucket_rows if row.lift_ratio is not None]
        calls = [
            row
            for row in labeled
            if float(row.predicted_score) >= 0.55
            and float(row.predicted_confidence) >= 0.45
        ]
        true_calls = [row for row in calls if row.realized_breakout]
        false_calls = [row for row in calls if not row.realized_breakout]
        negatives = [row for row in labeled if not row.realized_breakout]
        result[str(bucket)] = {
            "samples": len(bucket_rows),
            "labeled_samples": len(labeled),
            "breakouts": len(breakouts),
            "breakout_precision": (
                round(len(true_calls) / len(calls), 6) if calls else None
            ),
            "false_positive_rate": (
                round(len(false_calls) / len(negatives), 6)
                if negatives
                else None
            ),
            "mean_lift": round(sum(lifts) / len(lifts), 6) if lifts else None,
            "baseline_coverage": (
                round(len(lifts) / len(bucket_rows), 6) if bucket_rows else 0.0
            ),
        }
    return result


def refresh_trend_calibration(
    channel_profile_id: uuid.UUID,
    *,
    run_key: str,
    target_age_hours: int = 24,
) -> TrendCalibrationSnapshot:
    if target_age_hours not in AGE_BUCKETS:
        raise ValueError(f"target_age_hours must be one of {AGE_BUCKETS}")
    derive_trend_outcomes(channel_profile_id)
    with session_scope() as session:
        profile = session.get(ChannelProfile, channel_profile_id)
        if profile is None:
            raise ValueError(f"channel profile not found: {channel_profile_id}")
        existing = session.scalar(
            select(TrendCalibrationSnapshot).where(
                TrendCalibrationSnapshot.channel_profile_id == channel_profile_id,
                TrendCalibrationSnapshot.run_key == run_key,
            )
        )
        if existing is not None:
            return existing
        rows = _training_rows(
            session,
            channel_profile_id,
            target_age_hours=target_age_hours,
        )
        result = train_calibration(rows)
        version = int(
            session.scalar(
                select(func.coalesce(func.max(TrendCalibrationSnapshot.version), 0))
                .where(
                    TrendCalibrationSnapshot.channel_profile_id
                    == channel_profile_id
                )
            )
            or 0
        ) + 1
        window_metrics = _window_metrics(session, channel_profile_id)
        snapshot = TrendCalibrationSnapshot(
            channel_profile_id=channel_profile_id,
            version=version,
            run_key=run_key,
            algorithm=result.algorithm,
            status=result.status,
            training_cutoff=result.training_cutoff,
            sample_count=result.sample_count,
            training_sample_count=result.training_sample_count,
            validation_sample_count=result.validation_sample_count,
            feature_names=list(CALIBRATION_FEATURES),
            feature_means=result.feature_means,
            feature_scales=result.feature_scales,
            coefficients=result.coefficients,
            intercept=_decimal(result.intercept),
            blend_ratio=_decimal(result.blend_ratio),
            confidence=_decimal(result.confidence),
            validation_metrics={
                **result.validation_metrics,
                "target_age_hours": target_age_hours,
            },
            calibration_metrics={
                **result.calibration_metrics,
                "windows": window_metrics,
            },
        )
        session.add(snapshot)
        session.flush()

        opportunities = list(
            session.scalars(
                select(TrendOpportunity).where(
                    TrendOpportunity.channel_profile_id == channel_profile_id
                )
            )
        )
        for opportunity in opportunities:
            features = opportunity_features(
                score=float(opportunity.opportunity_score),
                confidence=float(opportunity.confidence),
                components=dict(opportunity.components or {}),
            )
            value, metadata = blended_score(features, result)
            opportunity.calibration_version = version
            opportunity.calibration_metadata = {
                **metadata,
                "status": result.status,
                "calibration_confidence": result.confidence,
            }
            opportunity.calibrated_score = (
                _decimal(value) if result.blend_ratio > 0 else None
            )

        event_type = (
            "trend.calibration.ready"
            if result.blend_ratio > 0
            else "trend.calibration.blend_disabled"
        )
        session.add(
            DomainEvent(
                aggregate_type="channel_profile",
                aggregate_id=str(channel_profile_id),
                event_type=event_type,
                payload={
                    "channel_profile_id": str(channel_profile_id),
                    "calibration_version": version,
                    "run_key": run_key,
                    "status": result.status,
                    "sample_count": result.sample_count,
                    "validation_sample_count": result.validation_sample_count,
                    "blend_ratio": result.blend_ratio,
                    "confidence": result.confidence,
                    "target_age_hours": target_age_hours,
                },
            )
        )
        return snapshot


def calibration_summary(channel_profile_id: uuid.UUID) -> dict[str, Any]:
    with session_scope() as session:
        profile = session.get(ChannelProfile, channel_profile_id)
        if profile is None:
            raise ValueError(f"channel profile not found: {channel_profile_id}")
        snapshot = session.scalar(
            select(TrendCalibrationSnapshot)
            .where(TrendCalibrationSnapshot.channel_profile_id == channel_profile_id)
            .order_by(TrendCalibrationSnapshot.version.desc())
            .limit(1)
        )
        attribution_total = int(
            session.scalar(
                select(func.count(TrendOutcomeAttribution.id)).where(
                    TrendOutcomeAttribution.channel_profile_id == channel_profile_id
                )
            )
            or 0
        )
        unattributed = int(
            session.scalar(
                select(func.count(TrendOutcomeAttribution.id)).where(
                    TrendOutcomeAttribution.channel_profile_id == channel_profile_id,
                    TrendOutcomeAttribution.status != "attributed",
                )
            )
            or 0
        )
        return {
            "channel_profile_id": str(channel_profile_id),
            "latest_calibration": (
                {
                    "version": snapshot.version,
                    "run_key": snapshot.run_key,
                    "algorithm": snapshot.algorithm,
                    "status": snapshot.status,
                    "sample_count": snapshot.sample_count,
                    "training_sample_count": snapshot.training_sample_count,
                    "validation_sample_count": snapshot.validation_sample_count,
                    "blend_ratio": float(snapshot.blend_ratio),
                    "confidence": float(snapshot.confidence),
                    "training_cutoff": snapshot.training_cutoff.isoformat(),
                    "validation_metrics": dict(snapshot.validation_metrics or {}),
                    "calibration_metrics": dict(snapshot.calibration_metrics or {}),
                    "feature_coefficients": dict(snapshot.coefficients or {}),
                }
                if snapshot
                else None
            ),
            "attribution": {
                "total_snapshots": attribution_total,
                "unattributed_snapshots": unattributed,
                "missing_attribution_rate": (
                    round(unattributed / attribution_total, 6)
                    if attribution_total
                    else None
                ),
            },
            "windows": _window_metrics(session, channel_profile_id),
        }


def opportunity_outcomes(
    opportunity_id: uuid.UUID,
) -> list[TrendOpportunityOutcome]:
    with session_scope() as session:
        return list(
            session.scalars(
                select(TrendOpportunityOutcome)
                .where(TrendOpportunityOutcome.trend_opportunity_id == opportunity_id)
                .order_by(TrendOpportunityOutcome.age_bucket_hours)
            )
        )
