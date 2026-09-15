from __future__ import annotations

import math
from dataclasses import dataclass


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


@dataclass(frozen=True, slots=True)
class CandidateSignals:
    clip_id: str
    duration_seconds: float
    feature_score: float = 0.0
    hook_score: float = 0.0
    payoff_score: float = 0.0
    surprise_score: float = 0.0
    rewatch_score: float = 0.0
    views: int = 0
    engaged_views: int = 0
    average_view_percentage: float = 0.0
    shares: int = 0
    comments: int = 0
    subscribers_gained: int = 0
    categories: tuple[str, ...] = ()
    tone: tuple[str, ...] = ()
    source_creator: str | None = None
    short_production_id: str | None = None
    short_publication_id: str | None = None


@dataclass(frozen=True, slots=True)
class ScoredCandidate:
    signals: CandidateSignals
    deterministic_score: float
    opening_score: float
    score_breakdown: dict[str, float]


def _rate(count: int, denominator: int, excellent_rate: float) -> float:
    if count <= 0 or denominator <= 0:
        return 0.0
    return _clamp((count / denominator) / excellent_rate)


def score_pool(candidates: list[CandidateSignals]) -> list[ScoredCandidate]:
    if not candidates:
        return []

    popularity_values = [max(item.engaged_views, item.views, 0) for item in candidates]
    max_popularity = max(popularity_values, default=0)
    max_log = math.log1p(max_popularity) if max_popularity > 0 else 1.0
    results: list[ScoredCandidate] = []

    for item in candidates:
        denominator = max(item.engaged_views, item.views, 1)
        popularity_raw = max(item.engaged_views, item.views, 0)
        popularity = math.log1p(popularity_raw) / max_log if popularity_raw else 0.0
        completion = _clamp(item.average_view_percentage / 100.0)
        feature = _clamp(item.feature_score / 100.0)
        hook = _clamp(item.hook_score / 100.0)
        payoff = _clamp(item.payoff_score / 100.0)
        surprise = _clamp(item.surprise_score / 100.0)
        rewatch = _clamp(item.rewatch_score / 100.0)
        share_rate = _rate(item.shares, denominator, 0.03)
        comment_rate = _rate(item.comments, denominator, 0.02)
        subscriber_rate = _rate(item.subscribers_gained, denominator, 0.01)

        deterministic = (
            feature * 0.22
            + popularity * 0.20
            + completion * 0.18
            + payoff * 0.10
            + rewatch * 0.10
            + share_rate * 0.08
            + surprise * 0.05
            + comment_rate * 0.04
            + subscriber_rate * 0.03
        )
        opening = (
            hook * 0.34
            + surprise * 0.18
            + feature * 0.16
            + completion * 0.14
            + popularity * 0.12
            + payoff * 0.06
        )
        breakdown = {
            "feature": round(feature, 6),
            "popularity": round(popularity, 6),
            "completion": round(completion, 6),
            "hook": round(hook, 6),
            "payoff": round(payoff, 6),
            "rewatch": round(rewatch, 6),
            "share_rate": round(share_rate, 6),
            "surprise": round(surprise, 6),
            "comment_rate": round(comment_rate, 6),
            "subscriber_rate": round(subscriber_rate, 6),
        }
        results.append(
            ScoredCandidate(
                signals=item,
                deterministic_score=round(_clamp(deterministic), 6),
                opening_score=round(_clamp(opening), 6),
                score_breakdown=breakdown,
            )
        )

    return sorted(
        results,
        key=lambda item: (-item.deterministic_score, -item.opening_score, item.signals.clip_id),
    )


def _overlap(left: tuple[str, ...], right: tuple[str, ...]) -> float:
    left_set = {item.casefold() for item in left if item}
    right_set = {item.casefold() for item in right if item}
    if not left_set or not right_set:
        return 0.0
    return len(left_set & right_set) / len(left_set | right_set)


def _sequence_value(candidate: ScoredCandidate, recent: list[ScoredCandidate]) -> float:
    value = candidate.deterministic_score
    if not recent:
        return value

    last = recent[-1]
    category_overlap = _overlap(candidate.signals.categories, last.signals.categories)
    tone_overlap = _overlap(candidate.signals.tone, last.signals.tone)
    value -= category_overlap * 0.16
    value -= tone_overlap * 0.07

    if (
        candidate.signals.source_creator
        and last.signals.source_creator
        and candidate.signals.source_creator.casefold() == last.signals.source_creator.casefold()
    ):
        value -= 0.08

    if category_overlap == 0:
        value += 0.035
    if len(recent) >= 2:
        previous = recent[-2]
        if _overlap(candidate.signals.categories, previous.signals.categories) > 0.7:
            value -= 0.05
    return value


def sequence_pool(
    candidates: list[ScoredCandidate],
    *,
    target_duration_seconds: float,
    min_segments: int,
    max_segments: int,
    target_segment_count: int | None = None,
) -> list[ScoredCandidate]:
    if min_segments < 1 or max_segments < min_segments:
        raise ValueError("invalid segment bounds")
    if len(candidates) < min_segments:
        raise ValueError(
            f"not enough eligible candidates: need at least {min_segments}, got {len(candidates)}"
        )

    pool = list(candidates)
    opener = max(
        pool,
        key=lambda item: (item.opening_score, item.deterministic_score, item.signals.clip_id),
    )
    selected = [opener]
    remaining = [item for item in pool if item.signals.clip_id != opener.signals.clip_id]
    duration = opener.signals.duration_seconds

    closer: ScoredCandidate | None = None
    if remaining:
        closer = max(
            remaining,
            key=lambda item: (
                item.signals.payoff_score / 100.0 * 0.4 + item.deterministic_score * 0.6,
                item.signals.clip_id,
            ),
        )
        remaining = [item for item in remaining if item.signals.clip_id != closer.signals.clip_id]

    desired_count = target_segment_count or max_segments
    desired_count = min(max(desired_count, min_segments), max_segments)
    reserve_for_closer = 1 if closer is not None else 0

    while remaining and len(selected) < desired_count - reserve_for_closer:
        minimum_before_closer = min_segments - reserve_for_closer
        if len(selected) >= minimum_before_closer and duration >= target_duration_seconds:
            break
        next_item = max(
            remaining,
            key=lambda item: (_sequence_value(item, selected), item.signals.clip_id),
        )
        selected.append(next_item)
        remaining = [
            item for item in remaining if item.signals.clip_id != next_item.signals.clip_id
        ]
        duration += next_item.signals.duration_seconds

    if closer is not None and len(selected) < max_segments:
        selected.append(closer)
        duration += closer.signals.duration_seconds

    while len(selected) < min_segments and remaining:
        next_item = max(
            remaining,
            key=lambda item: (_sequence_value(item, selected), item.signals.clip_id),
        )
        selected.append(next_item)
        remaining = [
            item for item in remaining if item.signals.clip_id != next_item.signals.clip_id
        ]
        duration += next_item.signals.duration_seconds

    if len(selected) < min_segments:
        raise ValueError("candidate sequencing could not satisfy minimum segment count")
    return selected[:max_segments]
