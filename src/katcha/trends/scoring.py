from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

_METRIC_WEIGHTS = {
    "views": 1.0,
    "reach": 1.0,
    "impressions": 0.8,
    "likes": 1.4,
    "score": 1.8,
    "upvotes": 1.8,
    "comments": 2.5,
    "replies": 2.0,
    "shares": 3.0,
    "saves": 2.5,
    "mentions": 1.5,
}
_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def _hours_between(later: datetime, earlier: datetime) -> float:
    return max(0.0, (later - earlier).total_seconds() / 3600.0)


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def normalize_term(value: str) -> str:
    return " ".join(_TOKEN_RE.findall(value.casefold()))


def canonical_topic_key(value: str) -> str:
    normalized = normalize_term(value)
    return normalized.replace(" ", "-")[:255]


@dataclass(frozen=True, slots=True)
class SignalSample:
    entity_key: str
    source_kind: str
    independence_key: str
    observed_at: datetime
    published_at: datetime | None
    metrics: Mapping[str, float]
    source_weight: float = 1.0


@dataclass(frozen=True, slots=True)
class TopicDescriptor:
    display_name: str
    aliases: Sequence[str] = ()
    tags: Sequence[str] = ()


@dataclass(frozen=True, slots=True)
class WatchConfig:
    interests: Sequence[str] = ()
    excluded_terms: Sequence[str] = ()
    freshness_horizon_hours: int = 72


@dataclass(frozen=True, slots=True)
class TrendScore:
    opportunity_score: float
    confidence: float
    lifecycle: str
    components: dict[str, float]
    reasons: tuple[str, ...]


def _metric_value(metrics: Mapping[str, float], key: str) -> float:
    try:
        return max(0.0, float(metrics.get(key, 0.0) or 0.0))
    except (TypeError, ValueError):
        return 0.0


def _activity(metrics: Mapping[str, float]) -> float:
    total = 0.0
    for key, weight in _METRIC_WEIGHTS.items():
        total += weight * math.log1p(_metric_value(metrics, key))
    return total


def _delta_activity(samples: Sequence[SignalSample]) -> list[tuple[SignalSample, float]]:
    by_entity: dict[str, list[SignalSample]] = {}
    for sample in samples:
        by_entity.setdefault(sample.entity_key, []).append(sample)

    increments: list[tuple[SignalSample, float]] = []
    for entity_samples in by_entity.values():
        ordered = sorted(entity_samples, key=lambda item: _utc(item.observed_at))
        previous: Mapping[str, float] | None = None
        for sample in ordered:
            if previous is None:
                activity = 0.35 * _activity(sample.metrics)
            else:
                delta = {
                    key: max(
                        0.0,
                        _metric_value(sample.metrics, key) - _metric_value(previous, key),
                    )
                    for key in _METRIC_WEIGHTS
                }
                activity = _activity(delta)
            increments.append((sample, activity * max(0.0, sample.source_weight)))
            previous = sample.metrics
    return increments


def _ratio_score(current_rate: float, prior_rate: float) -> float:
    ratio = (current_rate + 0.05) / (prior_rate + 0.05)
    return _clamp(math.log1p(ratio) / math.log(6.0))


def _interest_fit(topic: TopicDescriptor, watch: WatchConfig) -> tuple[float, bool]:
    corpus = " ".join(
        normalize_term(value)
        for value in (topic.display_name, *topic.aliases, *topic.tags)
        if value
    )
    excluded = [normalize_term(value) for value in watch.excluded_terms if value.strip()]
    if any(term and term in corpus for term in excluded):
        return 0.0, True

    interests = [normalize_term(value) for value in watch.interests if value.strip()]
    if not interests:
        return 0.5, False
    hits = sum(1 for term in interests if term and term in corpus)
    return _clamp(hits / max(1.0, min(4.0, float(len(interests))))), False


def score_topic(
    *,
    topic: TopicDescriptor,
    samples: Sequence[SignalSample],
    watch: WatchConfig,
    now: datetime | None = None,
) -> TrendScore:
    if not samples:
        return TrendScore(
            opportunity_score=0.0,
            confidence=0.0,
            lifecycle="emerging",
            components={},
            reasons=("no_signal_observations",),
        )

    now_utc = _utc(now or datetime.now(UTC))
    ordered = sorted(samples, key=lambda item: _utc(item.observed_at))
    increments = _delta_activity(ordered)

    short_hours = 6.0
    mid_hours = 24.0
    baseline_hours = 72.0
    recent_activity = 0.0
    prior_activity = 0.0
    baseline_activity = 0.0
    occupied_recent_buckets: set[int] = set()
    for sample, value in increments:
        age = _hours_between(now_utc, _utc(sample.observed_at))
        if age <= short_hours:
            recent_activity += value
        elif age <= mid_hours:
            prior_activity += value
        elif age <= baseline_hours:
            baseline_activity += value
        if age <= mid_hours and value > 0:
            occupied_recent_buckets.add(min(3, int(age // 6)))

    recent_rate = recent_activity / short_hours
    prior_rate = prior_activity / max(1.0, mid_hours - short_hours)
    baseline_rate = baseline_activity / max(1.0, baseline_hours - mid_hours)
    velocity = _ratio_score(recent_rate, prior_rate)
    breakout = _ratio_score(recent_rate, baseline_rate)
    acceleration = _clamp(
        0.5 + (velocity - _ratio_score(prior_rate, baseline_rate)) * 0.9
    )

    source_kinds = {sample.source_kind.casefold() for sample in ordered if sample.source_kind}
    independence = {
        sample.independence_key.casefold()
        for sample in ordered
        if sample.independence_key
    }
    breadth = _clamp(len(source_kinds) / 4.0)
    independence_score = _clamp(len(independence) / 4.0)

    first_at = min(_utc(sample.published_at or sample.observed_at) for sample in ordered)
    last_at = max(_utc(sample.observed_at) for sample in ordered)
    age_first = _hours_between(now_utc, first_at)
    age_latest = _hours_between(now_utc, last_at)
    novelty = math.exp(-age_first / 48.0)
    freshness = math.exp(
        -age_latest / max(1.0, float(watch.freshness_horizon_hours))
    )
    persistence = _clamp(len(occupied_recent_buckets) / 4.0)

    latest_by_entity: dict[str, SignalSample] = {}
    for sample in ordered:
        latest_by_entity[sample.entity_key] = sample
    comments = sum(
        _metric_value(item.metrics, "comments") + _metric_value(item.metrics, "replies")
        for item in latest_by_entity.values()
    )
    exposure = sum(
        max(
            _metric_value(item.metrics, "views"),
            _metric_value(item.metrics, "reach"),
            _metric_value(item.metrics, "impressions"),
            _metric_value(item.metrics, "score"),
            _metric_value(item.metrics, "upvotes"),
            _metric_value(item.metrics, "mentions"),
            1.0,
        )
        for item in latest_by_entity.values()
    )
    community_intensity = _clamp(
        math.log1p(100.0 * comments / exposure) / math.log(8.0)
    )

    metric_coverage = sum(
        1
        for key in _METRIC_WEIGHTS
        if any(_metric_value(sample.metrics, key) > 0 for sample in ordered)
    ) / len(_METRIC_WEIGHTS)
    depth = _clamp(len(ordered) / 10.0)
    data_quality = _clamp(0.55 * metric_coverage + 0.45 * depth)

    fit, excluded = _interest_fit(topic, watch)
    total_activity = recent_activity + prior_activity + baseline_activity
    volume_score = _clamp(math.log1p(total_activity) / math.log(80.0))
    age_pressure = _clamp(
        age_first / max(24.0, float(watch.freshness_horizon_hours) * 1.5)
    )
    saturation = _clamp(
        0.30 * age_pressure + 0.35 * volume_score + 0.35 * (1.0 - acceleration)
    )
    headroom = 1.0 - saturation

    confidence = _clamp(
        0.12
        + 0.26 * depth
        + 0.22 * breadth
        + 0.20 * independence_score
        + 0.20 * data_quality
    )
    if len(independence) <= 1:
        confidence = min(confidence, 0.55)
    elif len(independence) == 2:
        confidence = min(confidence, 0.78)
    elif len(independence) == 3:
        confidence = min(confidence, 0.92)

    components = {
        "velocity": velocity,
        "acceleration": acceleration,
        "breakout": breakout,
        "novelty": _clamp(novelty),
        "source_breadth": breadth,
        "source_independence": independence_score,
        "community_intensity": community_intensity,
        "persistence": persistence,
        "channel_fit": fit,
        "freshness": _clamp(freshness),
        "data_quality": data_quality,
        "saturation": saturation,
        "headroom": headroom,
    }

    positive = (
        0.18 * velocity
        + 0.18 * acceleration
        + 0.17 * breakout
        + 0.10 * novelty
        + 0.10 * breadth
        + 0.05 * independence_score
        + 0.06 * community_intensity
        + 0.05 * persistence
        + 0.07 * fit
        + 0.04 * freshness
    )
    opportunity = _clamp(
        (positive * (0.78 + 0.22 * confidence)) - 0.10 * saturation
    )
    if excluded:
        opportunity = 0.0

    if (
        age_latest <= mid_hours
        and (recent_rate < prior_rate * 0.55 or (acceleration <= 0.25 and velocity < 0.50))
    ):
        lifecycle = "cooling"
    elif saturation >= 0.76 and acceleration < 0.55:
        lifecycle = "saturated"
    elif breakout >= 0.72 and acceleration < 0.60:
        lifecycle = "peaking"
    elif (
        breakout >= 0.68
        and velocity >= 0.62
        and acceleration >= 0.60
        and breadth >= 0.25
    ):
        lifecycle = "breaking_out"
    elif acceleration >= 0.60 and velocity >= 0.55:
        lifecycle = "accelerating"
    else:
        lifecycle = "emerging"

    reasons: list[str] = []
    ranked_positive = sorted(
        (
            ("velocity", velocity),
            ("acceleration", acceleration),
            ("breakout", breakout),
            ("source_breadth", breadth),
            ("community_intensity", community_intensity),
            ("channel_fit", fit),
            ("headroom", headroom),
        ),
        key=lambda item: item[1],
        reverse=True,
    )
    reasons.extend(
        f"strong_{name}" for name, value in ranked_positive[:3] if value >= 0.55
    )
    if len(independence) <= 1:
        reasons.append("single_independent_source_confidence_cap")
    if excluded:
        reasons.append("excluded_by_watch_profile")
    if freshness < 0.35:
        reasons.append("stale_signal_penalty")
    if saturation > 0.70:
        reasons.append("saturation_penalty")
    if not reasons:
        reasons.append("insufficient_breakout_evidence")

    return TrendScore(
        opportunity_score=round(opportunity, 6),
        confidence=round(confidence, 6),
        lifecycle=lifecycle,
        components={key: round(value, 6) for key, value in components.items()},
        reasons=tuple(reasons),
    )
