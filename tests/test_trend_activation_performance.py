from __future__ import annotations

import inspect
from decimal import Decimal
from types import SimpleNamespace

from sqlalchemy import CheckConstraint, UniqueConstraint

from katcha.api.main import app
from katcha.orchestration.intelligence_workflows import (
    ChannelTrendActivationPerformanceWorkflow,
)
from katcha.services.trend_activation_performance import (
    recommend_activation_policy,
    refresh_activation_performance,
)
from katcha.trend_activation_models import TrendActivationPerformanceSnapshot


def _policy(**overrides):
    values = {
        "min_opportunity_score": Decimal("0.65"),
        "min_confidence": Decimal("0.55"),
        "min_rights_readiness": Decimal("0.60"),
        "max_activations_per_day": 3,
        "cooldown_minutes": 120,
        "max_backlog": 5,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _metrics(**overrides):
    values = {
        "published_count": 10,
        "outcome_count": 8,
        "revenue_coverage": 1.0,
        "plan_to_publish_rate": 0.85,
        "mean_lift_ratio": 1.15,
        "contribution_margin_usd": Decimal("25.00"),
        "capacity_miss_count": 2,
    }
    values.update(overrides)
    return values


def test_performance_snapshot_is_versioned_and_run_idempotent() -> None:
    constraints = TrendActivationPerformanceSnapshot.__table__.constraints
    uniques = {
        tuple(column.name for column in constraint.columns)
        for constraint in constraints
        if isinstance(constraint, UniqueConstraint)
    }
    checks = {
        constraint.name
        for constraint in constraints
        if isinstance(constraint, CheckConstraint)
    }

    assert ("channel_profile_id", "version") in uniques
    assert ("channel_profile_id", "run_key") in uniques
    assert "ck_trend_activation_performance_plan_rate" in checks
    assert "ck_trend_activation_performance_publish_rate" in checks


def test_recommendation_waits_for_sufficient_published_outcomes() -> None:
    status, recommendation = recommend_activation_policy(
        _metrics(published_count=4, outcome_count=2),
        _policy(),
    )

    assert status == "insufficient_data"
    assert recommendation["proposed_changes"] == {}


def test_recommendation_identifies_production_bottleneck_before_scaling() -> None:
    status, recommendation = recommend_activation_policy(
        _metrics(plan_to_publish_rate=0.45),
        _policy(),
    )

    assert status == "production_bottleneck"
    assert recommendation["proposed_changes"]["max_activations_per_day"] == 2
    assert recommendation["proposed_changes"]["max_backlog"] == 4


def test_recommendation_tightens_unprofitable_activation_quality() -> None:
    status, recommendation = recommend_activation_policy(
        _metrics(
            contribution_margin_usd=Decimal("-3.00"),
            mean_lift_ratio=0.92,
        ),
        _policy(),
    )

    changes = recommendation["proposed_changes"]
    assert status == "tighten_quality"
    assert changes["min_opportunity_score"] == 0.68
    assert changes["min_confidence"] == 0.57
    assert changes["max_activations_per_day"] == 2


def test_recommendation_expands_only_profitable_high_lift_capacity() -> None:
    status, recommendation = recommend_activation_policy(
        _metrics(),
        _policy(),
    )

    changes = recommendation["proposed_changes"]
    assert status == "expand_capacity"
    assert changes["max_activations_per_day"] == 4
    assert changes["max_backlog"] == 6
    assert changes["cooldown_minutes"] == 90


def test_recommendation_never_changes_rights_or_publication_safety_gates() -> None:
    scenarios = [
        _metrics(),
        _metrics(contribution_margin_usd=Decimal("-2"), mean_lift_ratio=0.8),
        _metrics(plan_to_publish_rate=0.4),
    ]
    forbidden = {
        "min_rights_readiness",
        "source_health",
        "review_gate",
        "publication_gate",
        "min_budget_headroom_usd",
    }
    for metrics in scenarios:
        _, recommendation = recommend_activation_policy(metrics, _policy())
        assert forbidden.isdisjoint(recommendation["proposed_changes"])


def test_low_revenue_coverage_does_not_treat_missing_revenue_as_zero() -> None:
    status, recommendation = recommend_activation_policy(
        _metrics(
            revenue_coverage=0.2,
            contribution_margin_usd=Decimal("-10.00"),
        ),
        _policy(),
    )

    assert status == "insufficient_economics"
    assert recommendation["proposed_changes"] == {}


def test_performance_routes_are_mounted() -> None:
    paths = set(app.openapi()["paths"])
    base = "/v1/channels/{channel_profile_id}/trends/activation-performance"
    assert f"{base}/refresh" in paths
    assert base in paths
    assert f"{base}/history" in paths


def test_performance_refresh_is_zero_token_and_advisory_only() -> None:
    source = inspect.getsource(refresh_activation_performance).casefold()
    assert "openai" not in source
    assert "gemini" not in source
    assert "create_activation_policy" not in source
    assert "activate_trend_opportunity" not in source


def test_performance_workflow_uses_existing_intelligence_infrastructure() -> None:
    assert (
        ChannelTrendActivationPerformanceWorkflow.__name__
        == "ChannelTrendActivationPerformanceWorkflow"
    )
