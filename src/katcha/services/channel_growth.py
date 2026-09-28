from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta
from typing import Any

from katcha.db import session_scope
from katcha.integrations.youtube.analytics import (
    YouTubeAnalyticsError,
    channel_growth_metrics,
)
from katcha.integrations.youtube.client import YouTubeAPIError, YouTubeClient
from katcha.models import DomainEvent
from katcha.services.channel_profiles import active_strategy, ensure_active_profile

ADS_THRESHOLD_CHANGE = date(2027, 2, 1)

DEFAULT_GROWTH_GOALS: dict[str, object] = {
    "objective": "ads_revenue",
    "path": "fastest",
    "pace": "aggressive",
    "target_date": None,
    "custom_targets": [],
}


def ypp_benchmarks(as_of: date) -> dict[str, object]:
    ads_watch_hours = 8000 if as_of >= ADS_THRESHOLD_CHANGE else 4000
    ads_shorts_views = 20_000_000 if as_of >= ADS_THRESHOLD_CHANGE else 10_000_000
    return {
        "effective_date": as_of.isoformat(),
        "early_ypp": {
            "subscribers": 500,
            "public_uploads_90d": 3,
            "watch_hours_365d": 3000,
            "shorts_views_90d": 3_000_000,
            "audience_requirement": "watch_hours_or_shorts_views",
        },
        "ads_premium": {
            "subscribers": 1000,
            "watch_hours_365d": ads_watch_hours,
            "shorts_views_90d": ads_shorts_views,
            "audience_requirement": "watch_hours_or_shorts_views",
        },
        "next_change": (
            None
            if as_of >= ADS_THRESHOLD_CHANGE
            else {
                "effective_date": ADS_THRESHOLD_CHANGE.isoformat(),
                "ads_premium": {
                    "subscribers": 1000,
                    "watch_hours_365d": 8000,
                    "shorts_views_90d": 20_000_000,
                },
            }
        ),
    }


def normalize_growth_goals(value: dict[str, object] | None) -> dict[str, object]:
    raw = dict(DEFAULT_GROWTH_GOALS)
    raw.update(dict(value or {}))
    if raw["objective"] not in {"early_ypp", "ads_revenue", "channel_growth"}:
        raise ValueError("unsupported growth objective")
    if raw["path"] not in {"fastest", "shorts", "long_form", "balanced"}:
        raise ValueError("unsupported channel growth path")
    if raw["pace"] not in {"aggressive", "balanced"}:
        raise ValueError("unsupported channel growth pace")
    custom = raw.get("custom_targets") or []
    if not isinstance(custom, list) or len(custom) > 20:
        raise ValueError("custom growth targets must be a list of at most 20 items")
    raw["custom_targets"] = custom
    return raw


def _ratio(value: float | int | None, target: float | int) -> float | None:
    if value is None:
        return None
    if target <= 0:
        return 1.0
    return max(0.0, min(float(value) / float(target), 1.0))


def _metric(
    key: str,
    current: float | int | None,
    target: float | int,
    *,
    estimated: bool = False,
) -> dict[str, object]:
    return {
        "metric": key,
        "current": current,
        "target": target,
        "progress": _ratio(current, target),
        "remaining": None if current is None else max(float(target) - float(current), 0),
        "estimated": estimated,
    }


def _milestone(
    key: str,
    thresholds: dict[str, object],
    metrics: dict[str, object],
) -> dict[str, object]:
    subscriber = _metric(
        "subscribers",
        metrics.get("subscribers"),
        int(thresholds["subscribers"]),
    )
    watch = _metric(
        "qualified_watch_hours_365d",
        metrics.get("estimated_qualified_watch_hours_365d"),
        int(thresholds["watch_hours_365d"]),
        estimated=True,
    )
    shorts = _metric(
        "qualified_shorts_views_90d",
        metrics.get("estimated_qualified_shorts_views_90d"),
        int(thresholds["shorts_views_90d"]),
        estimated=True,
    )
    requirements = [subscriber]
    upload = None
    if "public_uploads_90d" in thresholds:
        upload = _metric(
            "public_uploads_90d",
            metrics.get("public_uploads_90d"),
            int(thresholds["public_uploads_90d"]),
        )
        requirements.append(upload)

    subscriber_met = subscriber["current"] is not None and subscriber["progress"] == 1.0
    upload_met = upload is None or (
        upload["current"] is not None and upload["progress"] == 1.0
    )
    audience_known = watch["current"] is not None or shorts["current"] is not None
    audience_met = (
        (watch["current"] is not None and watch["progress"] == 1.0)
        or (shorts["current"] is not None and shorts["progress"] == 1.0)
    )
    requirements_known = all(item["progress"] is not None for item in requirements)
    progress_values = [
        float(item["progress"])
        for item in requirements
        if item["progress"] is not None
    ]
    audience_progress = max(
        [float(item["progress"]) for item in (watch, shorts) if item["progress"] is not None],
        default=0.0,
    )
    if audience_known:
        progress_values.append(audience_progress)
    milestone_progress = (
        min(progress_values)
        if requirements_known and audience_known and progress_values
        else None
    )

    return {
        "key": key,
        "thresholds": thresholds,
        "requirements": requirements,
        "audience_paths": [watch, shorts],
        "progress": milestone_progress,
        "thresholds_met_estimate": bool(
            subscriber_met and upload_met and audience_known and audience_met
        ),
    }


def _select_path(
    goals: dict[str, object],
    milestone: dict[str, object],
) -> str:
    requested = str(goals.get("path") or "fastest")
    if requested != "fastest":
        return requested
    paths = {item["metric"]: item for item in milestone["audience_paths"]}
    watch = paths["qualified_watch_hours_365d"]["progress"]
    shorts = paths["qualified_shorts_views_90d"]["progress"]
    if watch is None and shorts is None:
        return "balanced"
    if shorts is None:
        return "long_form"
    if watch is None:
        return "shorts"
    return "shorts" if float(shorts) > float(watch) else "long_form"


def evaluate_growth_progress(
    metrics: dict[str, object],
    *,
    goals: dict[str, object] | None = None,
    as_of: date,
) -> dict[str, object]:
    normalized = normalize_growth_goals(goals)
    benchmarks = ypp_benchmarks(as_of)
    early = _milestone(
        "early_ypp",
        dict(benchmarks["early_ypp"]),
        metrics,
    )
    ads = _milestone(
        "ads_premium",
        dict(benchmarks["ads_premium"]),
        metrics,
    )
    target = early if normalized["objective"] == "early_ypp" else ads
    if ads["thresholds_met_estimate"]:
        stage = "ads_thresholds_met"
    elif early["thresholds_met_estimate"]:
        stage = "early_ypp_thresholds_met"
    else:
        stage = "pre_ypp"

    selected_path = _select_path(normalized, target)
    required = list(target["requirements"])
    paths = {item["metric"]: item for item in target["audience_paths"]}
    if selected_path == "shorts":
        required.append(paths["qualified_shorts_views_90d"])
    elif selected_path == "long_form":
        required.append(paths["qualified_watch_hours_365d"])
    else:
        required.extend(target["audience_paths"])

    metric_sources = {
        "subscribers": metrics.get("subscribers"),
        "public_uploads_90d": metrics.get("public_uploads_90d"),
        "qualified_watch_hours_365d": metrics.get(
            "estimated_qualified_watch_hours_365d"
        ),
        "qualified_shorts_views_90d": metrics.get(
            "estimated_qualified_shorts_views_90d"
        ),
    }
    for raw in normalized.get("custom_targets") or []:
        if not isinstance(raw, dict) or not raw.get("enabled", True):
            continue
        metric_key = str(raw.get("metric") or "")
        if metric_key not in metric_sources:
            continue
        try:
            target_value = float(raw["target"])
        except (KeyError, TypeError, ValueError):
            continue
        custom_metric = _metric(
            metric_key,
            metric_sources[metric_key],
            target_value,
            estimated=metric_key.startswith("qualified_"),
        )
        custom_metric["custom"] = True
        custom_metric["priority"] = int(raw.get("priority") or 3)
        required.append(custom_metric)

    priorities = sorted(
        [item for item in required if item["progress"] is None or item["progress"] < 1],
        key=lambda item: (
            -int(item.get("priority") or 3),
            -1 if item["progress"] is None else float(item["progress"]),
        ),
    )
    priority_metrics = list(dict.fromkeys(item["metric"] for item in priorities))
    return {
        "as_of": as_of.isoformat(),
        "benchmarks": benchmarks,
        "goals": normalized,
        "milestones": {
            "early_ypp": early,
            "ads_premium": ads,
        },
        "ai": {
            "stage": stage,
            "selected_path": selected_path,
            "pace": normalized["pace"],
            "priority_metrics": priority_metrics,
        },
    }


def _growth_signal(features: dict[str, float], path: str) -> float:
    def value(key: str) -> float:
        return max(0.0, min(float(features.get(key, 0.0)), 1.0))

    shorts = (
        value("hook_score") * 0.30
        + value("rewatch_potential") * 0.30
        + value("surprise_score") * 0.20
        + value("comment_potential") * 0.20
    )
    long_form = (
        value("hook_score") * 0.40
        + value("duration_signal") * 0.30
        + value("baseline_score") * 0.20
        + value("comment_potential") * 0.10
    )
    if path == "shorts":
        return shorts
    if path == "long_form":
        return long_form
    return (shorts + long_form) / 2.0


def apply_growth_priority(
    score: float,
    features: dict[str, float],
    growth_context: dict[str, object] | None,
) -> tuple[float, dict[str, object]]:
    if not growth_context:
        return score, {"growth_weight": 0.0}
    ai = dict(growth_context.get("ai") or {})
    goals = dict(growth_context.get("goals") or {})
    if ai.get("stage") == "ads_thresholds_met" and goals.get("objective") == "ads_revenue":
        return score, {
            "growth_weight": 0.0,
            "growth_stage": ai.get("stage"),
            "growth_path": ai.get("selected_path"),
        }

    weight = 0.18 if ai.get("pace") == "aggressive" else 0.08
    signal = _growth_signal(features, str(ai.get("selected_path") or "balanced"))
    blended = max(0.0, min(score * (1.0 - weight) + signal * weight, 1.0))
    return blended, {
        "growth_weight": weight,
        "growth_signal": signal,
        "growth_stage": ai.get("stage"),
        "growth_path": ai.get("selected_path"),
        "growth_priorities": list(ai.get("priority_metrics") or []),
    }


def channel_growth_context(channel_profile_id: uuid.UUID) -> dict[str, object]:
    with session_scope() as session:
        profile = ensure_active_profile(session, channel_profile_id)
        strategy = active_strategy(session, profile)
        goals = normalize_growth_goals(
            dict(strategy.strategy_metadata or {}).get("growth")
        )
        snapshot = dict(profile.profile_metadata or {}).get("growth_snapshot")
        if isinstance(snapshot, dict):
            snapshot_metrics = dict(snapshot.get("metrics") or {})
            as_of_raw = str(snapshot.get("as_of") or date.today().isoformat())
            try:
                as_of = date.fromisoformat(as_of_raw)
            except ValueError:
                as_of = date.today()
            recalculated = evaluate_growth_progress(
                snapshot_metrics,
                goals=goals,
                as_of=as_of,
            )
            return {**snapshot, **recalculated, "metrics": snapshot_metrics}
        return evaluate_growth_progress({}, goals=goals, as_of=date.today())


def refresh_channel_growth(
    channel_profile_id: uuid.UUID,
    *,
    now: datetime | None = None,
) -> dict[str, object]:
    reference = (now or datetime.now(UTC)).astimezone(UTC)
    with session_scope() as session:
        profile = ensure_active_profile(session, channel_profile_id)
        connection_id = profile.youtube_connection_id
        strategy = active_strategy(session, profile)
        goals = normalize_growth_goals(
            dict(strategy.strategy_metadata or {}).get("growth")
        )

    metrics: dict[str, object] = {
        "subscribers": None,
        "public_uploads_90d": None,
        "estimated_qualified_watch_hours_365d": None,
        "estimated_qualified_shorts_views_90d": None,
    }
    coverage = {"channel_statistics": False, "analytics": False}
    errors: list[str] = []

    client = YouTubeClient(connection_id)
    try:
        channel = client.channel_resource()
        statistics = dict(channel.get("statistics") or {})
        if statistics.get("subscriberCount") is not None:
            metrics["subscribers"] = int(statistics["subscriberCount"])
        metrics["public_uploads_90d"] = client.public_uploads_since(
            reference - timedelta(days=89)
        )
        coverage["channel_statistics"] = True
    except (YouTubeAPIError, ValueError, TypeError) as exc:
        errors.append(f"channel_statistics: {str(exc)[:240]}")

    try:
        analytics = channel_growth_metrics(
            connection_id,
            as_of=reference.date(),
        )
        metrics["estimated_qualified_watch_hours_365d"] = round(
            float(analytics["estimated_qualified_watch_hours_365d"]),
            2,
        )
        metrics["estimated_qualified_shorts_views_90d"] = int(
            analytics["estimated_qualified_shorts_views_90d"]
        )
        coverage["analytics"] = True
    except (YouTubeAnalyticsError, ValueError, TypeError) as exc:
        errors.append(f"analytics: {str(exc)[:240]}")

    snapshot = {
        **evaluate_growth_progress(metrics, goals=goals, as_of=reference.date()),
        "sampled_at": reference.isoformat(),
        "metrics": metrics,
        "coverage": coverage,
        "errors": errors,
        "source": "youtube_api_estimate",
        "disclaimer": (
            "YouTube does not expose the exact qualified YPP counters shown in "
            "Studio. Watch-hour and Shorts-view values are Katcha estimates."
        ),
    }
    with session_scope() as session:
        profile = ensure_active_profile(session, channel_profile_id)
        metadata = dict(profile.profile_metadata or {})
        metadata["growth_snapshot"] = snapshot
        profile.profile_metadata = metadata
        session.add(
            DomainEvent(
                aggregate_type="channel_profile",
                aggregate_id=str(profile.id),
                event_type="channel_profile.growth_refreshed",
                payload={
                    "channel_profile_id": str(profile.id),
                    "stage": snapshot["ai"]["stage"],
                    "selected_path": snapshot["ai"]["selected_path"],
                    "coverage": coverage,
                },
            )
        )
    return snapshot
