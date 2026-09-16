from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime

ALGORITHM_VERSION = "emerging-trend-v1"
_METRIC_WEIGHTS = {
    "views": 0.01,
    "likes": 1.0,
    "comments": 3.0,
    "shares": 4.0,
    "upvotes": 1.0,
    "score": 1.0,
}
_VELOCITY_SCALE = 10_000.0


@dataclass(frozen=True, slots=True)
class TrendObservation:
    observed_at: datetime
    published_at: datetime | None
    source_key: str
    metrics: Mapping[str, float | int]


@dataclass(frozen=True, slots=True)
class TrendResult:
    score: float
    freshness: float
    velocity: float
    acceleration: float
    corroboration: float
    novelty: float
    saturation_penalty: float
    observation_count: int
    source_count: int
    window_start: datetime | None
    window_end: datetime | None

    def breakdown(self) -> dict[str, float | int | str | None]:
        return {
            "algorithm_version": ALGORITHM_VERSION,
            "score": self.score,
            "freshness": self.freshness,
            "velocity": self.velocity,
            "acceleration": self.acceleration,
            "corroboration": self.corroboration,
            "novelty": self.novelty,
            "saturation_penalty": self.saturation_penalty,
            "observation_count": self.observation_count,
            "source_count": self.source_count,
            "window_start": self.window_start.isoformat() if self.window_start else None,
            "window_end": self.window_end.isoformat() if self.window_end else None,
        }


def _clamp(value: float) -> float:
    return max(0.0, min(float(value), 1.0))


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def engagement_index(metrics: Mapping[str, float | int]) -> float:
    total = 0.0
    for key, weight in _METRIC_WEIGHTS.items():
        try:
            value = float(metrics.get(key, 0) or 0)
        except (TypeError, ValueError):
            value = 0.0
        total += max(value, 0.0) * weight
    return total


def _normalized_rate(rate_per_hour: float) -> float:
    if rate_per_hour <= 0:
        return 0.0
    return _clamp(math.log1p(rate_per_hour) / math.log1p(_VELOCITY_SCALE))


def _velocity(samples: list[TrendObservation]) -> tuple[float, float]:
    if len(samples) < 2:
        return 0.0, 0.0

    first = samples[0]
    last = samples[-1]
    elapsed_hours = max(
        (_utc(last.observed_at) - _utc(first.observed_at)).total_seconds() / 3600.0,
        1.0 / 60.0,
    )
    first_value = engagement_index(first.metrics)
    last_value = engagement_index(last.metrics)
    overall_rate = max(last_value - first_value, 0.0) / elapsed_hours
    velocity = _normalized_rate(overall_rate)

    if len(samples) < 3:
        return velocity, 0.0

    midpoint = samples[len(samples) // 2]
    first_hours = max(
        (_utc(midpoint.observed_at) - _utc(first.observed_at)).total_seconds() / 3600.0,
        1.0 / 60.0,
    )
    second_hours = max(
        (_utc(last.observed_at) - _utc(midpoint.observed_at)).total_seconds() / 3600.0,
        1.0 / 60.0,
    )
    midpoint_value = engagement_index(midpoint.metrics)
    early_rate = max(midpoint_value - first_value, 0.0) / first_hours
    late_rate = max(last_value - midpoint_value, 0.0) / second_hours
    acceleration = _normalized_rate(max(late_rate - early_rate, 0.0))
    return velocity, acceleration


def score_trend(
    observations: list[TrendObservation],
    *,
    now: datetime | None = None,
    freshness_horizon_hours: int = 72,
    related_item_count: int = 0,
    corroborating_source_count: int | None = None,
    saturation_target: int = 12,
) -> TrendResult:
    if freshness_horizon_hours <= 0:
        raise ValueError("freshness_horizon_hours must be positive")
    if saturation_target <= 0:
        raise ValueError("saturation_target must be positive")

    ordered = sorted(observations, key=lambda item: _utc(item.observed_at))
    timestamp = _utc(now or datetime.now(UTC))
    observed_sources = {item.source_key for item in ordered if item.source_key}
    source_count = max(
        len(observed_sources),
        int(corroborating_source_count or 0),
    )

    published_values = [
        _utc(item.published_at)
        for item in ordered
        if item.published_at is not None
    ]
    if published_values:
        published_at = min(published_values)
    elif ordered:
        published_at = _utc(ordered[0].observed_at)
    else:
        published_at = timestamp

    age_hours = max((timestamp - published_at).total_seconds() / 3600.0, 0.0)
    half_life = max(freshness_horizon_hours / 3.0, 1.0)
    freshness = _clamp(2 ** (-age_hours / half_life))
    velocity, acceleration = _velocity(ordered)
    corroboration = _clamp(max(source_count - 1, 0) / 3.0)
    related = max(int(related_item_count), 0)
    novelty = _clamp(1.0 - related / saturation_target)
    saturation_penalty = min(0.45, 0.45 * related / saturation_target)

    raw = (
        freshness * 0.34
        + velocity * 0.30
        + acceleration * 0.12
        + corroboration * 0.14
        + novelty * 0.10
    )
    score = _clamp(raw - saturation_penalty)
    return TrendResult(
        score=round(score, 6),
        freshness=round(freshness, 6),
        velocity=round(velocity, 6),
        acceleration=round(acceleration, 6),
        corroboration=round(corroboration, 6),
        novelty=round(novelty, 6),
        saturation_penalty=round(saturation_penalty, 6),
        observation_count=len(ordered),
        source_count=source_count,
        window_start=_utc(ordered[0].observed_at) if ordered else None,
        window_end=_utc(ordered[-1].observed_at) if ordered else None,
    )
