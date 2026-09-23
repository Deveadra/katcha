from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import CheckConstraint, UniqueConstraint

from katcha.api.main import app
from katcha.trend_calibration_models import (
    TrendCalibrationSnapshot,
    TrendOpportunityOutcome,
    TrendOutcomeAttribution,
)
from katcha.trend_models import TrendOpportunity
from katcha.trends.calibration import (
    MAX_CALIBRATION_BLEND,
    MIN_CALIBRATION_SAMPLES,
    CalibrationRow,
    blended_score,
    opportunity_features,
    train_calibration,
)


def _unique_columns(table) -> set[tuple[str, ...]]:
    return {
        tuple(column.name for column in constraint.columns)
        for constraint in table.constraints
        if isinstance(constraint, UniqueConstraint)
    }


def _check_names(table) -> set[str]:
    return {
        constraint.name
        for constraint in table.constraints
        if isinstance(constraint, CheckConstraint) and constraint.name is not None
    }


def _row(
    index: int,
    *,
    baseline: float,
    outcome: float,
    confidence: float = 0.7,
) -> CalibrationRow:
    return CalibrationRow(
        observed_at=datetime(2026, 1, 1, tzinfo=UTC) + timedelta(days=index),
        features=opportunity_features(
            score=baseline,
            confidence=confidence,
            components={
                "velocity": baseline,
                "acceleration": baseline,
                "breakout": baseline,
                "novelty": 0.8,
                "source_breadth": 0.5,
                "source_independence": 0.5,
                "community_intensity": 0.4,
                "persistence": 0.5,
                "channel_fit": 0.8,
                "freshness": 0.9,
                "data_quality": 0.8,
                "headroom": 0.7,
            },
        ),
        outcome=outcome,
        realized_breakout=outcome >= 0.625,
        lead_time_hours=float(index + 1),
        lifecycle="accelerating",
    )


def test_calibration_schema_is_versioned_and_idempotent() -> None:
    assert ("analytics_snapshot_id",) in _unique_columns(
        TrendOutcomeAttribution.__table__
    )
    assert ("analytics_snapshot_id",) in _unique_columns(
        TrendOpportunityOutcome.__table__
    )
    calibration_unique = _unique_columns(TrendCalibrationSnapshot.__table__)
    assert ("channel_profile_id", "version") in calibration_unique
    assert ("channel_profile_id", "run_key") in calibration_unique
    assert "ck_trend_calibration_blend_ratio" in _check_names(
        TrendCalibrationSnapshot.__table__
    )
    assert "ck_trend_opportunity_calibrated_score" in _check_names(
        TrendOpportunity.__table__
    )


def test_insufficient_samples_disable_learned_blend() -> None:
    rows = [
        _row(index, baseline=0.4 + index * 0.01, outcome=0.5)
        for index in range(MIN_CALIBRATION_SAMPLES - 1)
    ]
    result = train_calibration(rows)
    assert result.status == "insufficient_samples"
    assert result.blend_ratio == 0.0
    assert result.confidence == 0.0


def test_chronological_holdout_never_trains_on_future_validation_rows() -> None:
    rows = [
        _row(
            index,
            baseline=0.25 + index * 0.02,
            outcome=min(1.0, 0.20 + index * 0.03),
        )
        for index in range(MIN_CALIBRATION_SAMPLES + 8)
    ]
    result = train_calibration(rows)
    assert result.validation_sample_count >= 4
    assert result.validation_metrics["chronological_holdout"] is True
    training_end = datetime.fromisoformat(
        str(result.validation_metrics["training_end"])
    )
    validation_start = datetime.fromisoformat(
        str(result.validation_metrics["validation_start"])
    )
    assert training_end < validation_start
    assert result.blend_ratio <= MAX_CALIBRATION_BLEND


def test_perfect_deterministic_baseline_keeps_learned_blend_disabled() -> None:
    rows = [
        _row(
            index,
            baseline=0.3 + (index % 5) * 0.1,
            outcome=0.3 + (index % 5) * 0.1,
        )
        for index in range(MIN_CALIBRATION_SAMPLES + 10)
    ]
    result = train_calibration(rows)
    assert result.status == "validation_failed"
    assert result.blend_ratio == 0.0
    assert result.validation_metrics["baseline_mae"] == 0.0


def test_disabled_calibration_returns_deterministic_score_unchanged() -> None:
    rows = [
        _row(index, baseline=0.6, outcome=0.6)
        for index in range(MIN_CALIBRATION_SAMPLES + 4)
    ]
    result = train_calibration(rows)
    score, metadata = blended_score(
        opportunity_features(score=0.73, confidence=0.8, components={}),
        result,
    )
    assert score == 0.73
    assert metadata["blend_ratio"] == 0.0


def test_calibration_routes_are_mounted() -> None:
    paths = set(app.openapi()["paths"])
    assert "/v1/trends/channels/{channel_profile_id}/calibration/refresh" in paths
    assert "/v1/trends/channels/{channel_profile_id}/calibration" in paths
    assert "/v1/trends/opportunities/{opportunity_id}/outcomes" in paths
