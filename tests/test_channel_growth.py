from datetime import date

from katcha.services.channel_growth import (
    apply_growth_priority,
    evaluate_growth_progress,
    ypp_benchmarks,
)


def _metrics() -> dict[str, object]:
    return {
        "subscribers": 620,
        "public_uploads_90d": 4,
        "estimated_qualified_watch_hours_365d": 1000.0,
        "estimated_qualified_shorts_views_90d": 2_500_000,
    }


def test_ypp_benchmarks_are_effective_dated() -> None:
    current = ypp_benchmarks(date(2026, 9, 28))
    future = ypp_benchmarks(date(2027, 2, 1))

    assert current["early_ypp"]["subscribers"] == 500
    assert current["early_ypp"]["public_uploads_90d"] == 3
    assert current["ads_premium"]["watch_hours_365d"] == 4000
    assert current["ads_premium"]["shorts_views_90d"] == 10_000_000
    assert current["next_change"]["effective_date"] == "2027-02-01"

    assert future["ads_premium"]["watch_hours_365d"] == 8000
    assert future["ads_premium"]["shorts_views_90d"] == 20_000_000
    assert future["next_change"] is None


def test_growth_progress_selects_fastest_measured_path() -> None:
    result = evaluate_growth_progress(
        _metrics(),
        goals={
            "objective": "early_ypp",
            "path": "fastest",
            "pace": "aggressive",
        },
        as_of=date(2026, 9, 28),
    )

    assert result["milestones"]["early_ypp"]["thresholds_met_estimate"] is False
    assert result["ai"]["selected_path"] == "shorts"
    assert result["ai"]["pace"] == "aggressive"
    assert "qualified_shorts_views_90d" in result["ai"]["priority_metrics"]


def test_growth_progress_does_not_fake_progress_for_unknown_counters() -> None:
    result = evaluate_growth_progress(
        {"public_uploads_90d": 3},
        as_of=date(2026, 9, 28),
    )

    assert result["milestones"]["early_ypp"]["progress"] is None
    assert result["milestones"]["ads_premium"]["progress"] is None
    assert result["milestones"]["early_ypp"]["thresholds_met_estimate"] is False


def test_custom_growth_goal_becomes_ai_priority() -> None:
    result = evaluate_growth_progress(
        _metrics(),
        goals={
            "objective": "ads_revenue",
            "path": "long_form",
            "pace": "aggressive",
            "custom_targets": [
                {
                    "metric": "subscribers",
                    "target": 5000,
                    "priority": 5,
                    "enabled": True,
                }
            ],
        },
        as_of=date(2026, 9, 28),
    )

    assert result["ai"]["priority_metrics"][0] == "subscribers"


def test_aggressive_growth_weight_is_bounded_and_stronger_than_balanced() -> None:
    features = {
        "baseline_score": 0.2,
        "hook_score": 1.0,
        "rewatch_potential": 1.0,
        "surprise_score": 1.0,
        "comment_potential": 1.0,
    }
    aggressive, aggressive_details = apply_growth_priority(
        0.2,
        features,
        {
            "goals": {"objective": "ads_revenue"},
            "ai": {
                "stage": "pre_ypp",
                "selected_path": "shorts",
                "pace": "aggressive",
                "priority_metrics": ["subscribers"],
            },
        },
    )
    balanced, balanced_details = apply_growth_priority(
        0.2,
        features,
        {
            "goals": {"objective": "ads_revenue"},
            "ai": {
                "stage": "pre_ypp",
                "selected_path": "shorts",
                "pace": "balanced",
                "priority_metrics": ["subscribers"],
            },
        },
    )

    assert 0.0 <= balanced < aggressive <= 1.0
    assert aggressive_details["growth_weight"] == 0.18
    assert balanced_details["growth_weight"] == 0.08
