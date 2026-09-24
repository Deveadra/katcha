from __future__ import annotations

import inspect
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

from sqlalchemy import CheckConstraint, UniqueConstraint

from katcha.api.main import app
from katcha.orchestration.intelligence_workflows import (
    ChannelTrendActivationScheduleWorkflow,
    ChannelTrendActivationWorkflow,
)
from katcha.services.trend_auto_activation import (
    _decision_reason,
    create_activation_policy,
    run_autonomous_trend_activation,
)
from katcha.trend_activation_models import (
    TrendActivationDecision,
    TrendActivationPolicyVersion,
    TrendActivationRun,
)


def _unique_columns(table) -> set[tuple[str, ...]]:
    return {
        tuple(column.name for column in constraint.columns)
        for constraint in table.constraints
        if isinstance(constraint, UniqueConstraint)
    }


def _check_names(table) -> set[str]:
    return {
        constraint.name
        for constraint in table.constraints
        if isinstance(constraint, CheckConstraint) and constraint.name is not None
    }


def _opportunity(**overrides):
    values = {
        "id": uuid.uuid4(),
        "channel_profile_id": uuid.uuid4(),
        "opportunity_score": Decimal("0.80"),
        "confidence": Decimal("0.75"),
        "calibrated_score": Decimal("0.77"),
        "components": {"rights_readiness": 0.9},
        "expires_at": datetime(2026, 9, 24, 18, 0, tzinfo=UTC),
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _policy(**overrides):
    values = {
        "min_opportunity_score": Decimal("0.65"),
        "min_confidence": Decimal("0.55"),
        "min_calibrated_score": Decimal("0.70"),
        "min_rights_readiness": Decimal("0.60"),
        "min_lead_time_minutes": 120,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_autonomous_activation_schema_is_versioned_and_idempotent() -> None:
    policy_unique = _unique_columns(TrendActivationPolicyVersion.__table__)
    run_unique = _unique_columns(TrendActivationRun.__table__)
    decision_unique = _unique_columns(TrendActivationDecision.__table__)

    assert ("channel_profile_id", "version") in policy_unique
    assert ("channel_profile_id", "run_key") in run_unique
    assert ("activation_run_id", "trend_opportunity_id") in decision_unique
    assert "ck_trend_activation_policy_min_score" in _check_names(
        TrendActivationPolicyVersion.__table__
    )
    assert "ck_trend_activation_policy_budget_headroom" in _check_names(
        TrendActivationPolicyVersion.__table__
    )


def test_policy_gate_accepts_strong_current_opportunity() -> None:
    now = datetime(2026, 9, 24, 12, 0, tzinfo=UTC)
    decision, reason, readiness, lead = _decision_reason(
        _opportunity(),
        _policy(),
        now=now,
    )

    assert decision is None
    assert reason is None
    assert readiness == 0.9
    assert lead == 360


def test_policy_gate_fails_closed_on_missing_calibrated_score() -> None:
    now = datetime(2026, 9, 24, 12, 0, tzinfo=UTC)
    decision, reason, _, _ = _decision_reason(
        _opportunity(calibrated_score=None),
        _policy(),
        now=now,
    )

    assert decision == "calibrated_unavailable"
    assert reason == "calibrated_score_required_but_unavailable"


def test_policy_gate_checks_rights_and_remaining_lead_time() -> None:
    now = datetime(2026, 9, 24, 12, 0, tzinfo=UTC)
    decision, reason, _, _ = _decision_reason(
        _opportunity(components={"rights_readiness": 0.2}),
        _policy(),
        now=now,
    )
    assert decision == "rights_readiness"
    assert reason == "rights_readiness_below_policy"

    decision, reason, _, lead = _decision_reason(
        _opportunity(expires_at=now + timedelta(minutes=30)),
        _policy(),
        now=now,
    )
    assert decision == "lead_time"
    assert reason == "insufficient_remaining_lead_time"
    assert lead == 30


def test_autonomous_activation_routes_are_mounted() -> None:
    paths = set(app.openapi()["paths"])
    assert "/v1/channels/{channel_profile_id}/trends/activation-policy" in paths
    assert "/v1/channels/{channel_profile_id}/trends/activation-scan" in paths
    assert "/v1/channels/{channel_profile_id}/trends/activation-schedule" in paths
    assert "/v1/channels/{channel_profile_id}/trends/activation-runs" in paths
    assert "/v1/trends/activation-runs/{run_id}/decisions" in paths


def test_scan_reuses_p7_1_activation_service_and_does_not_start_editorial() -> None:
    source = inspect.getsource(run_autonomous_trend_activation)
    assert "preview_trend_activation" in source
    assert "activate_trend_opportunity" in source
    assert "start_short_episode_editorial_workflow" not in source
    assert "openai" not in source.casefold()
    assert "gemini" not in source.casefold()


def test_policy_service_is_explicitly_versioned_per_channel() -> None:
    parameters = inspect.signature(create_activation_policy).parameters
    assert "channel_profile_id" in parameters
    assert "enabled" in parameters
    assert "max_activations_per_day" in parameters
    assert "min_budget_headroom_usd" in parameters


def test_temporal_workflows_are_registered_types() -> None:
    assert ChannelTrendActivationWorkflow.__name__ == "ChannelTrendActivationWorkflow"
    assert (
        ChannelTrendActivationScheduleWorkflow.__name__
        == "ChannelTrendActivationScheduleWorkflow"
    )
