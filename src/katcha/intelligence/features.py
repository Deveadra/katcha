from __future__ import annotations

from typing import Any

from katcha.intelligence.learning import clamp
from katcha.models import Clip, ClipFeature


def active_ai_features(features: ClipFeature | dict[str, Any]) -> dict[str, Any]:
    raw = features.ai_features if isinstance(features, ClipFeature) else features.get("ai_features")
    if not isinstance(raw, dict):
        return {}
    deep = raw.get("deep")
    bulk = raw.get("bulk")
    if isinstance(deep, dict):
        return deep
    if isinstance(bulk, dict):
        return bulk
    return {}


def score01(value: object) -> float:
    try:
        return clamp(float(value or 0) / 100.0)
    except (TypeError, ValueError):
        return 0.0


def clip_learning_features(clip: Clip, features: ClipFeature) -> dict[str, float]:
    ai = active_ai_features(features)
    duration = float(clip.duration_seconds or 0)
    return {
        "baseline_score": clamp(float(features.candidate_score or 0) / 100.0),
        "hook_score": score01(ai.get("hook_score")),
        "surprise_score": score01(ai.get("surprise_score")),
        "humor_score": score01(ai.get("humor_score")),
        "comment_potential": score01(ai.get("comment_potential")),
        "rewatch_potential": score01(ai.get("rewatch_potential")),
        "duration_signal": clamp(duration / 60.0),
    }


def snapshot_learning_features(snapshot: dict[str, Any]) -> dict[str, float]:
    ai = active_ai_features(snapshot)
    try:
        candidate_score = float(snapshot.get("candidate_score") or 0)
    except (TypeError, ValueError):
        candidate_score = 0.0
    try:
        duration = float(snapshot.get("duration_seconds") or 0)
    except (TypeError, ValueError):
        duration = 0.0
    return {
        "baseline_score": clamp(candidate_score / 100.0),
        "hook_score": score01(ai.get("hook_score")),
        "surprise_score": score01(ai.get("surprise_score")),
        "humor_score": score01(ai.get("humor_score")),
        "comment_potential": score01(ai.get("comment_potential")),
        "rewatch_potential": score01(ai.get("rewatch_potential")),
        "duration_signal": clamp(duration / 60.0),
    }
