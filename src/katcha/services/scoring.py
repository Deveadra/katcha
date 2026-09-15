from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class ScoreResult:
    score: float
    breakdown: dict[str, float]


def _clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, value))


def _number(metadata: dict[str, Any], *keys: str) -> float | None:
    for key in keys:
        value = metadata.get(key)
        if value is None:
            continue
        try:
            return float(value)
        except (TypeError, ValueError):
            continue
    return None


def _source_signal(source_metrics: dict[str, Any]) -> tuple[float, float]:
    views = _number(source_metrics, "view_count", "views", "play_count")
    likes = _number(source_metrics, "like_count", "likes") or 0.0
    comments = _number(source_metrics, "comment_count", "comments") or 0.0

    if views is None or views <= 0:
        return 50.0, 50.0

    view_score = _clamp((math.log10(max(views, 1.0)) / 7.0) * 100.0)
    engagement_rate = (likes + (comments * 2.0)) / views
    engagement_score = _clamp(engagement_rate * 1000.0)
    return view_score, engagement_score


def score_candidate(
    *,
    local_features: dict[str, Any],
    source_metrics: dict[str, Any],
) -> ScoreResult:
    duration = float(local_features.get("duration_seconds") or 0.0)
    if duration <= 0:
        duration_score = 40.0
    elif duration <= 25:
        duration_score = 100.0
    elif duration <= 60:
        duration_score = 100.0 - (((duration - 25.0) / 35.0) * 35.0)
    else:
        duration_score = 55.0

    mean_hash_distance = float(local_features.get("mean_hash_distance") or 0.0)
    visual_change_score = _clamp(mean_hash_distance * 5.0)

    transcript_words = float(local_features.get("transcript_word_count") or 0.0)
    speech_density = transcript_words / max(duration, 1.0)
    speech_score = _clamp(speech_density * 30.0)

    view_score, engagement_score = _source_signal(source_metrics)
    breakdown = {
        "source_reach": round(view_score, 3),
        "source_engagement": round(engagement_score, 3),
        "duration_fit": round(duration_score, 3),
        "visual_change": round(visual_change_score, 3),
        "speech_density": round(speech_score, 3),
    }
    score = (
        (view_score * 0.30)
        + (engagement_score * 0.25)
        + (duration_score * 0.15)
        + (visual_change_score * 0.20)
        + (speech_score * 0.10)
    )
    return ScoreResult(score=round(_clamp(score), 3), breakdown=breakdown)
