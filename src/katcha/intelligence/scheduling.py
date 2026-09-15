from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from statistics import fmean

from katcha.intelligence.learning import clamp

MIN_HISTORY_FOR_OPTIMIZATION = 6
PRIOR_STRENGTH = 4.0


@dataclass(frozen=True, slots=True)
class HistoricalSlot:
    weekday: int
    hour_local: int
    outcome_score: float


@dataclass(frozen=True, slots=True)
class RecommendedWindow:
    weekday: int
    hour_local: int
    score: float
    sample_count: int
    confidence: float
    source: str


def _valid_slot(weekday: int, hour_local: int) -> bool:
    return 0 <= weekday <= 6 and 0 <= hour_local <= 23


def _fallback_windows(
    fallback_schedule: list[dict[str, object]],
    *,
    limit: int,
    existing: set[tuple[int, int]] | None = None,
) -> list[RecommendedWindow]:
    seen = set(existing or set())
    result: list[RecommendedWindow] = []
    for raw in fallback_schedule:
        try:
            weekday = int(raw["weekday"])
            hour_local = int(raw["hour_local"])
        except (KeyError, TypeError, ValueError):
            continue
        key = (weekday, hour_local)
        if key in seen or not _valid_slot(weekday, hour_local):
            continue
        seen.add(key)
        result.append(
            RecommendedWindow(
                weekday=weekday,
                hour_local=hour_local,
                score=0.0,
                sample_count=0,
                confidence=0.0,
                source="configured_fallback",
            )
        )
        if len(result) >= limit:
            break
    return result


def recommend_windows(
    history: Iterable[HistoricalSlot],
    *,
    fallback_schedule: list[dict[str, object]] | None = None,
    limit: int = 5,
) -> list[RecommendedWindow]:
    if limit < 1:
        raise ValueError("schedule recommendation limit must be positive")
    samples = [
        item
        for item in history
        if _valid_slot(item.weekday, item.hour_local)
    ]
    fallback = fallback_schedule or []
    if len(samples) < MIN_HISTORY_FOR_OPTIMIZATION:
        return _fallback_windows(fallback, limit=limit)

    global_mean = fmean(clamp(item.outcome_score) for item in samples)
    grouped: dict[tuple[int, int], list[float]] = defaultdict(list)
    for item in samples:
        grouped[(item.weekday, item.hour_local)].append(clamp(item.outcome_score))

    history_confidence = clamp(len(samples) / 30.0)
    ranked: list[RecommendedWindow] = []
    for (weekday, hour_local), outcomes in grouped.items():
        sample_count = len(outcomes)
        mean = fmean(outcomes)
        shrunk = (
            (sample_count * mean) + (PRIOR_STRENGTH * global_mean)
        ) / (sample_count + PRIOR_STRENGTH)
        slot_confidence = sample_count / (sample_count + 5.0)
        confidence = clamp(slot_confidence * history_confidence)
        ranked.append(
            RecommendedWindow(
                weekday=weekday,
                hour_local=hour_local,
                score=round(clamp(shrunk), 6),
                sample_count=sample_count,
                confidence=round(confidence, 6),
                source="channel_history",
            )
        )

    ranked.sort(
        key=lambda item: (
            -item.score,
            -item.confidence,
            -item.sample_count,
            item.weekday,
            item.hour_local,
        )
    )
    selected = ranked[:limit]
    if len(selected) < limit:
        selected.extend(
            _fallback_windows(
                fallback,
                limit=limit - len(selected),
                existing={(item.weekday, item.hour_local) for item in selected},
            )
        )
    return selected[:limit]
