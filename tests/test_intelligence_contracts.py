from sqlalchemy import CheckConstraint, UniqueConstraint

from katcha.ai.router import ROUTES
from katcha.api.intelligence import EconomicsResponse, StrategyResponse
from katcha.api.main import app
from katcha.domain import AITask
from katcha.intelligence_models import (
    AIBudgetReservation,
    ChannelStrategyVersion,
    RankingSnapshot,
)
from katcha.longform_models import Compilation
from katcha.production_models import Production


def test_editorial_outputs_expose_channel_scope() -> None:
    assert Production.__table__.c.channel_profile_id.nullable is True
    assert Compilation.__table__.c.channel_profile_id.nullable is True


def test_ranking_snapshot_run_key_is_unique_per_channel() -> None:
    unique_columns = {
        tuple(column.name for column in constraint.columns)
        for constraint in RankingSnapshot.__table__.constraints
        if isinstance(constraint, UniqueConstraint)
    }

    assert ("channel_profile_id", "run_key") in unique_columns
    assert ("channel_profile_id", "version") in unique_columns


def test_budget_reservations_are_unique_per_channel_and_key() -> None:
    unique_columns = {
        tuple(column.name for column in constraint.columns)
        for constraint in AIBudgetReservation.__table__.constraints
        if isinstance(constraint, UniqueConstraint)
    }

    assert ("channel_profile_id", "reservation_key") in unique_columns


def test_strategy_enforces_base_budget_within_hard_ceiling() -> None:
    checks = {
        constraint.name
        for constraint in ChannelStrategyVersion.__table__.constraints
        if isinstance(constraint, CheckConstraint)
    }

    assert "ck_strategy_base_budget_nonnegative" in checks
    assert "ck_strategy_budget_nonnegative" in checks
    assert "ck_strategy_base_within_hard_budget" in checks


def test_tts_has_channel_routing_route() -> None:
    route = ROUTES[AITask.TTS]

    assert route.primary.provider == "openai"
    assert route.fallback is not None
    assert route.fallback.provider == "gemini"


def test_control_schemas_expose_budget_semantics() -> None:
    strategy_fields = set(StrategyResponse.model_fields)
    economics_fields = set(EconomicsResponse.model_fields)

    assert {"monthly_base_budget_usd", "monthly_hard_budget_usd"} <= strategy_fields
    assert {
        "base_budget_usd",
        "hard_budget_usd",
        "effective_budget_usd",
        "reserved_ai_cost_usd",
        "burn_rate_usd_per_day",
        "projected_month_end_spend_usd",
    } <= economics_fields


def test_intelligence_control_routes_are_mounted() -> None:
    paths = {route.path for route in app.routes}

    assert "/v1/channels" in paths
    assert "/v1/channels/{channel_profile_id}/intelligence/refresh" in paths
    assert "/v1/channels/{channel_profile_id}/clips/{clip_id}/score" in paths
    assert "/v1/control/events" in paths
    assert "/v1/control/events/{event_id}/ack" in paths
