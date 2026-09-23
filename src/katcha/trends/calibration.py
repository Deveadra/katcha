from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, datetime
from statistics import fmean, median
from typing import Any

CALIBRATION_FEATURES: tuple[str, ...] = (
    "baseline_score",
    "confidence",
    "velocity",
    "acceleration",
    "breakout",
    "novelty",
    "source_breadth",
    "source_independence",
    "community_intensity",
    "persistence",
    "channel_fit",
    "freshness",
    "data_quality",
    "headroom",
)

MIN_CALIBRATION_SAMPLES = 20
MIN_TRAINING_SAMPLES = 16
MIN_VALIDATION_SAMPLES = 4
MAX_CALIBRATION_BLEND = 0.35
RIDGE_LAMBDA = 1.5


def clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


@dataclass(frozen=True, slots=True)
class CalibrationRow:
    observed_at: datetime
    features: dict[str, float]
    outcome: float
    realized_breakout: bool | None = None
    lead_time_hours: float | None = None
    lifecycle: str | None = None


@dataclass(frozen=True, slots=True)
class CalibrationResult:
    algorithm: str
    status: str
    sample_count: int
    training_sample_count: int
    validation_sample_count: int
    feature_names: tuple[str, ...]
    feature_means: dict[str, float]
    feature_scales: dict[str, float]
    coefficients: dict[str, float]
    intercept: float
    confidence: float
    blend_ratio: float
    validation_metrics: dict[str, Any]
    calibration_metrics: dict[str, Any]
    training_cutoff: datetime


def opportunity_features(
    *,
    score: float,
    confidence: float,
    components: dict[str, float],
) -> dict[str, float]:
    result = {
        "baseline_score": clamp(float(score)),
        "confidence": clamp(float(confidence)),
    }
    for name in CALIBRATION_FEATURES:
        if name in {"baseline_score", "confidence"}:
            continue
        value = components.get(name, 0.0)
        try:
            result[name] = clamp(float(value))
        except (TypeError, ValueError):
            result[name] = 0.0
    return result


def _feature(features: dict[str, float], name: str) -> float:
    try:
        return float(features.get(name, 0.0))
    except (TypeError, ValueError):
        return 0.0


def _means_scales(
    rows: list[CalibrationRow],
) -> tuple[dict[str, float], dict[str, float]]:
    means: dict[str, float] = {}
    scales: dict[str, float] = {}
    for name in CALIBRATION_FEATURES:
        values = [_feature(row.features, name) for row in rows]
        mean = fmean(values) if values else 0.0
        variance = fmean((value - mean) ** 2 for value in values) if values else 0.0
        scale = math.sqrt(variance)
        means[name] = mean
        scales[name] = scale if scale > 1e-9 else 1.0
    return means, scales


def _vector(
    features: dict[str, float],
    means: dict[str, float],
    scales: dict[str, float],
) -> list[float]:
    return [
        (_feature(features, name) - means[name]) / scales[name]
        for name in CALIBRATION_FEATURES
    ]


def _solve(matrix: list[list[float]], target: list[float]) -> list[float]:
    size = len(target)
    augmented = [row[:] + [target[index]] for index, row in enumerate(matrix)]
    for column in range(size):
        pivot = max(range(column, size), key=lambda row: abs(augmented[row][column]))
        if abs(augmented[pivot][column]) < 1e-12:
            continue
        augmented[column], augmented[pivot] = augmented[pivot], augmented[column]
        divisor = augmented[column][column]
        augmented[column] = [value / divisor for value in augmented[column]]
        for row in range(size):
            if row == column:
                continue
            factor = augmented[row][column]
            if abs(factor) < 1e-12:
                continue
            augmented[row] = [
                value - factor * pivot_value
                for value, pivot_value in zip(
                    augmented[row],
                    augmented[column],
                    strict=True,
                )
            ]
    return [augmented[index][-1] for index in range(size)]


def _fit(
    rows: list[CalibrationRow],
) -> tuple[dict[str, float], dict[str, float], dict[str, float], float]:
    means, scales = _means_scales(rows)
    vectors = [_vector(row.features, means, scales) for row in rows]
    outcomes = [clamp(row.outcome) for row in rows]
    intercept = fmean(outcomes) if outcomes else 0.0
    centered = [value - intercept for value in outcomes]
    count = len(CALIBRATION_FEATURES)
    gram = [[0.0 for _ in range(count)] for _ in range(count)]
    rhs = [0.0 for _ in range(count)]
    for vector, outcome in zip(vectors, centered, strict=True):
        for left in range(count):
            rhs[left] += vector[left] * outcome
            for right in range(count):
                gram[left][right] += vector[left] * vector[right]
    for index in range(count):
        gram[index][index] += RIDGE_LAMBDA
    values = _solve(gram, rhs)
    coefficients = {
        name: values[index] for index, name in enumerate(CALIBRATION_FEATURES)
    }
    return means, scales, coefficients, intercept


def predict(
    features: dict[str, float],
    *,
    means: dict[str, float],
    scales: dict[str, float],
    coefficients: dict[str, float],
    intercept: float,
) -> float:
    value = intercept
    for name in CALIBRATION_FEATURES:
        scale = float(scales.get(name, 1.0) or 1.0)
        standardized = (_feature(features, name) - float(means.get(name, 0.0))) / scale
        value += standardized * float(coefficients.get(name, 0.0))
    return clamp(value)


def _mae(actual: list[float], predicted: list[float]) -> float:
    if not actual:
        return 0.0
    return fmean(
        abs(left - right)
        for left, right in zip(actual, predicted, strict=True)
    )


def _metrics(rows: list[CalibrationRow]) -> dict[str, Any]:
    labeled = [row for row in rows if row.realized_breakout is not None]
    positives = [row for row in labeled if row.realized_breakout]
    negatives = [row for row in labeled if not row.realized_breakout]
    called = [row for row in labeled if _feature(row.features, "baseline_score") >= 0.55]
    true_positive = sum(1 for row in called if row.realized_breakout)
    false_positive = sum(1 for row in called if not row.realized_breakout)
    precision = true_positive / len(called) if called else None
    false_positive_rate = false_positive / len(negatives) if negatives else None
    leads = [
        float(row.lead_time_hours)
        for row in positives
        if row.lead_time_hours is not None
    ]
    confidence_bins: dict[str, dict[str, float | int | None]] = {}
    for low in (0.0, 0.25, 0.5, 0.75):
        high = low + 0.25
        bucket = [
            row
            for row in labeled
            if low <= _feature(row.features, "confidence")
            < (high if high < 1.0 else 1.000001)
        ]
        realized = sum(1 for row in bucket if row.realized_breakout)
        confidence_bins[f"{low:.2f}-{high:.2f}"] = {
            "count": len(bucket),
            "realized_rate": round(realized / len(bucket), 6) if bucket else None,
        }
    lifecycle: dict[str, dict[str, float | int]] = {}
    for row in labeled:
        key = row.lifecycle or "unknown"
        entry = lifecycle.setdefault(key, {"count": 0, "breakouts": 0})
        entry["count"] += 1
        if row.realized_breakout:
            entry["breakouts"] += 1
    for entry in lifecycle.values():
        count = int(entry["count"])
        entry["precision"] = round(int(entry["breakouts"]) / count, 6) if count else 0.0
    return {
        "labeled_samples": len(labeled),
        "realized_breakouts": len(positives),
        "breakout_precision": round(precision, 6) if precision is not None else None,
        "false_positive_rate": (
            round(false_positive_rate, 6)
            if false_positive_rate is not None
            else None
        ),
        "mean_lead_time_hours": round(fmean(leads), 4) if leads else None,
        "median_lead_time_hours": round(median(leads), 4) if leads else None,
        "confidence_buckets": confidence_bins,
        "lifecycle_precision": lifecycle,
    }


def train_calibration(rows: list[CalibrationRow]) -> CalibrationResult:
    samples = sorted(rows, key=lambda row: row.observed_at)
    count = len(samples)
    cutoff = samples[-1].observed_at if samples else datetime.min.replace(tzinfo=UTC)
    metrics = _metrics(samples)
    if count < MIN_CALIBRATION_SAMPLES:
        return CalibrationResult(
            algorithm="deterministic-only-v1",
            status="insufficient_samples",
            sample_count=count,
            training_sample_count=count,
            validation_sample_count=0,
            feature_names=CALIBRATION_FEATURES,
            feature_means={},
            feature_scales={},
            coefficients={},
            intercept=0.0,
            confidence=0.0,
            blend_ratio=0.0,
            validation_metrics={
                "reason": "insufficient_samples",
                "minimum_samples": MIN_CALIBRATION_SAMPLES,
                "sample_count": count,
            },
            calibration_metrics=metrics,
            training_cutoff=cutoff,
        )

    validation_count = max(MIN_VALIDATION_SAMPLES, int(count * 0.20))
    split = count - validation_count
    if split < MIN_TRAINING_SAMPLES:
        return CalibrationResult(
            algorithm="deterministic-only-v1",
            status="insufficient_samples",
            sample_count=count,
            training_sample_count=split,
            validation_sample_count=validation_count,
            feature_names=CALIBRATION_FEATURES,
            feature_means={},
            feature_scales={},
            coefficients={},
            intercept=0.0,
            confidence=0.0,
            blend_ratio=0.0,
            validation_metrics={
                "reason": "insufficient_training_samples",
                "minimum_training_samples": MIN_TRAINING_SAMPLES,
                "training_sample_count": split,
            },
            calibration_metrics=metrics,
            training_cutoff=samples[max(0, split - 1)].observed_at,
        )
    training = samples[:split]
    validation = samples[split:]
    means, scales, coefficients, intercept = _fit(training)
    actual = [clamp(row.outcome) for row in validation]
    learned = [
        predict(
            row.features,
            means=means,
            scales=scales,
            coefficients=coefficients,
            intercept=intercept,
        )
        for row in validation
    ]
    baseline = [clamp(_feature(row.features, "baseline_score")) for row in validation]
    learned_mae = _mae(actual, learned)
    baseline_mae = _mae(actual, baseline)
    improvement = (
        max(0.0, 1.0 - learned_mae / baseline_mae)
        if baseline_mae > 1e-9
        else 0.0
    )
    sample_confidence = clamp((count - MIN_CALIBRATION_SAMPLES + 1) / 50.0)
    confidence = clamp(improvement * sample_confidence)
    blend_ratio = min(MAX_CALIBRATION_BLEND, confidence * MAX_CALIBRATION_BLEND)
    status = "active" if blend_ratio > 0 else "validation_failed"

    return CalibrationResult(
        algorithm="trend-ridge-v1",
        status=status,
        sample_count=count,
        training_sample_count=len(training),
        validation_sample_count=len(validation),
        feature_names=CALIBRATION_FEATURES,
        feature_means={key: round(value, 8) for key, value in means.items()},
        feature_scales={key: round(value, 8) for key, value in scales.items()},
        coefficients={key: round(value, 8) for key, value in coefficients.items()},
        intercept=round(intercept, 8),
        confidence=round(confidence, 6),
        blend_ratio=round(blend_ratio, 6),
        validation_metrics={
            "learned_mae": round(learned_mae, 8),
            "baseline_mae": round(baseline_mae, 8),
            "improvement": round(improvement, 8),
            "chronological_holdout": True,
            "training_end": training[-1].observed_at.isoformat(),
            "validation_start": validation[0].observed_at.isoformat(),
        },
        calibration_metrics=metrics,
        training_cutoff=training[-1].observed_at,
    )


def blended_score(
    features: dict[str, float],
    result: CalibrationResult,
) -> tuple[float, dict[str, float | str]]:
    baseline = clamp(_feature(features, "baseline_score"))
    if result.blend_ratio <= 0 or not result.coefficients:
        return baseline, {
            "baseline": round(baseline, 6),
            "learned": round(baseline, 6),
            "blend_ratio": 0.0,
            "algorithm": result.algorithm,
        }
    learned = predict(
        features,
        means=result.feature_means,
        scales=result.feature_scales,
        coefficients=result.coefficients,
        intercept=result.intercept,
    )
    score = baseline * (1.0 - result.blend_ratio) + learned * result.blend_ratio
    return round(clamp(score), 6), {
        "baseline": round(baseline, 6),
        "learned": round(learned, 6),
        "blend_ratio": round(result.blend_ratio, 6),
        "algorithm": result.algorithm,
    }
