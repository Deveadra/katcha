from __future__ import annotations

import math
from dataclasses import dataclass
from statistics import fmean
from typing import Iterable

FEATURE_NAMES: tuple[str, ...] = (
    "baseline_score",
    "hook_score",
    "surprise_score",
    "humor_score",
    "comment_potential",
    "rewatch_potential",
    "duration_signal",
)

MIN_LEARNED_SAMPLES = 12
MAX_LEARNED_BLEND = 0.65
RIDGE_LAMBDA = 1.0


def clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def outcome_score(
    *,
    views: int,
    engaged_views: int,
    age_hours: float,
    average_view_percentage: float,
    shares: int,
    comments: int,
    subscribers_gained: int,
) -> tuple[float, dict[str, float]]:
    audience = max(views, engaged_views, 0)
    denominator = max(audience, 1)
    age = max(age_hours, 1.0)
    views_per_hour = audience / age
    velocity = clamp(math.log1p(views_per_hour) / math.log1p(1000.0))
    completion = clamp(average_view_percentage / 100.0)
    share_rate = clamp((shares / denominator) / 0.03)
    comment_rate = clamp((comments / denominator) / 0.02)
    subscriber_rate = clamp((subscribers_gained / denominator) / 0.01)
    score = (
        velocity * 0.35
        + completion * 0.30
        + share_rate * 0.15
        + comment_rate * 0.10
        + subscriber_rate * 0.10
    )
    labels = {
        "views_per_hour": round(views_per_hour, 6),
        "velocity_signal": round(velocity, 6),
        "completion_signal": round(completion, 6),
        "share_rate_signal": round(share_rate, 6),
        "comment_rate_signal": round(comment_rate, 6),
        "subscriber_rate_signal": round(subscriber_rate, 6),
    }
    return round(clamp(score), 6), labels


@dataclass(frozen=True, slots=True)
class TrainingRow:
    features: dict[str, float]
    outcome: float


@dataclass(frozen=True, slots=True)
class TrainingResult:
    algorithm: str
    sample_count: int
    feature_names: tuple[str, ...]
    feature_means: dict[str, float]
    feature_scales: dict[str, float]
    coefficients: dict[str, float]
    intercept: float
    confidence: float
    blend_ratio: float
    validation_metrics: dict[str, float | int | str]


def _feature_value(features: dict[str, float], name: str) -> float:
    try:
        return float(features.get(name, 0.0))
    except (TypeError, ValueError):
        return 0.0


def _means_scales(rows: list[TrainingRow]) -> tuple[dict[str, float], dict[str, float]]:
    means: dict[str, float] = {}
    scales: dict[str, float] = {}
    for name in FEATURE_NAMES:
        values = [_feature_value(row.features, name) for row in rows]
        mean = fmean(values) if values else 0.0
        variance = fmean([(value - mean) ** 2 for value in values]) if values else 0.0
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
        (_feature_value(features, name) - means[name]) / scales[name]
        for name in FEATURE_NAMES
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
                for value, pivot_value in zip(augmented[row], augmented[column], strict=True)
            ]
    return [augmented[index][-1] for index in range(size)]


def _fit(rows: list[TrainingRow]) -> tuple[dict[str, float], dict[str, float], dict[str, float], float]:
    means, scales = _means_scales(rows)
    vectors = [_vector(row.features, means, scales) for row in rows]
    outcomes = [clamp(float(row.outcome)) for row in rows]
    intercept = fmean(outcomes) if outcomes else 0.0
    centered = [value - intercept for value in outcomes]
    feature_count = len(FEATURE_NAMES)
    gram = [[0.0 for _ in range(feature_count)] for _ in range(feature_count)]
    rhs = [0.0 for _ in range(feature_count)]
    for vector, outcome in zip(vectors, centered, strict=True):
        for left in range(feature_count):
            rhs[left] += vector[left] * outcome
            for right in range(feature_count):
                gram[left][right] += vector[left] * vector[right]
    for index in range(feature_count):
        gram[index][index] += RIDGE_LAMBDA
    coefficient_values = _solve(gram, rhs)
    coefficients = {
        name: coefficient_values[index]
        for index, name in enumerate(FEATURE_NAMES)
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
    for name in FEATURE_NAMES:
        scale = float(scales.get(name, 1.0) or 1.0)
        standardized = (_feature_value(features, name) - float(means.get(name, 0.0))) / scale
        value += standardized * float(coefficients.get(name, 0.0))
    return clamp(value)


def _mae(actual: list[float], predicted: list[float]) -> float:
    if not actual:
        return 0.0
    return fmean(abs(left - right) for left, right in zip(actual, predicted, strict=True))


def train_ranking(rows: Iterable[TrainingRow]) -> TrainingResult:
    samples = list(rows)
    count = len(samples)
    if count < MIN_LEARNED_SAMPLES:
        return TrainingResult(
            algorithm="baseline-only-v1",
            sample_count=count,
            feature_names=FEATURE_NAMES,
            feature_means={},
            feature_scales={},
            coefficients={},
            intercept=0.0,
            confidence=0.0,
            blend_ratio=0.0,
            validation_metrics={
                "reason": "insufficient_samples",
                "minimum_samples": MIN_LEARNED_SAMPLES,
                "sample_count": count,
            },
        )

    split = min(max(10, int(count * 0.80)), count - 2)
    training = samples[:split]
    validation = samples[split:]
    train_means, train_scales, train_coefficients, train_intercept = _fit(training)
    actual = [clamp(row.outcome) for row in validation]
    learned_predictions = [
        predict(
            row.features,
            means=train_means,
            scales=train_scales,
            coefficients=train_coefficients,
            intercept=train_intercept,
        )
        for row in validation
    ]
    baseline_predictions = [clamp(_feature_value(row.features, "baseline_score")) for row in validation]
    learned_mae = _mae(actual, learned_predictions)
    baseline_mae = _mae(actual, baseline_predictions)
    improvement = (
        clamp(1.0 - (learned_mae / baseline_mae))
        if baseline_mae > 1e-9
        else 0.0
    )
    sample_confidence = clamp((count - MIN_LEARNED_SAMPLES + 1) / 40.0)
    confidence = clamp(sample_confidence * improvement)
    blend_ratio = min(MAX_LEARNED_BLEND, confidence * MAX_LEARNED_BLEND)

    means, scales, coefficients, intercept = _fit(samples)
    return TrainingResult(
        algorithm="ridge-v1",
        sample_count=count,
        feature_names=FEATURE_NAMES,
        feature_means={key: round(value, 8) for key, value in means.items()},
        feature_scales={key: round(value, 8) for key, value in scales.items()},
        coefficients={key: round(value, 8) for key, value in coefficients.items()},
        intercept=round(intercept, 8),
        confidence=round(confidence, 6),
        blend_ratio=round(blend_ratio, 6),
        validation_metrics={
            "validation_samples": len(validation),
            "learned_mae": round(learned_mae, 8),
            "baseline_mae": round(baseline_mae, 8),
            "improvement": round(improvement, 8),
            "sample_confidence": round(sample_confidence, 8),
        },
    )


def blended_score(
    features: dict[str, float],
    result: TrainingResult,
) -> tuple[float, dict[str, float | str]]:
    baseline = clamp(_feature_value(features, "baseline_score"))
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
