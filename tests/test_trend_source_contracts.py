from __future__ import annotations

import uuid

import pytest
from sqlalchemy import CheckConstraint, UniqueConstraint

from katcha.api.main import app
from katcha.services.trend_sources import _assert_no_secrets, available_source_adapters
from katcha.trend_source_models import TrendSourcePoll, TrendSourceSubscription


def _unique_columns(table: object) -> set[tuple[str, ...]]:
    return {
        tuple(column.name for column in constraint.columns)
        for constraint in table.constraints
        if isinstance(constraint, UniqueConstraint)
    }


def _check_names(table: object) -> set[str]:
    return {
        constraint.name
        for constraint in table.constraints
        if isinstance(constraint, CheckConstraint) and constraint.name is not None
    }


def test_trend_source_subscription_is_channel_scoped_and_durable() -> None:
    table = TrendSourceSubscription.__table__
    assert ("channel_profile_id", "subscription_key") in _unique_columns(table)
    assert "ck_trend_source_poll_interval_minimum" in _check_names(table)
    assert "ck_trend_source_failures_nonnegative" in _check_names(table)
    assert table.c.cursor.nullable is False
    assert table.c.health_status.nullable is False
    assert table.c.next_poll_at.nullable is True
    assert table.c.backoff_until.nullable is True


def test_trend_source_poll_is_idempotent_per_subscription_and_run() -> None:
    table = TrendSourcePoll.__table__
    assert ("trend_source_subscription_id", "run_key") in _unique_columns(table)
    assert "ck_trend_source_poll_item_count" in _check_names(table)
    assert table.c.cursor_before.nullable is False
    assert table.c.cursor_after.nullable is False


def test_live_adapter_registry_exposes_required_providers() -> None:
    adapters = {(item["key"], item["version"]) for item in available_source_adapters()}
    assert ("reddit", "v1") in adapters
    assert ("youtube", "v1") in adapters
    assert ("rss", "v1") in adapters


def test_source_query_rejects_secret_bearing_fields() -> None:
    with pytest.raises(ValueError, match="secret-bearing field"):
        _assert_no_secrets(
            {
                "query": "gaming",
                "nested": {"api_key": "do-not-store-this"},
            }
        )


def test_live_trend_source_routes_are_mounted_once() -> None:
    paths = [route.path for route in app.routes]
    expected = {
        "/v1/trends/adapters",
        "/v1/channels/{channel_profile_id}/trends/sources",
        "/v1/channels/{channel_profile_id}/trends/sources/health",
        "/v1/trends/sources/{source_id}",
        "/v1/trends/sources/{source_id}/status",
        "/v1/trends/sources/{source_id}/poll",
    }
    for path in expected:
        assert paths.count(path) >= 1


def test_poll_model_uses_uuid_primary_key() -> None:
    poll = TrendSourcePoll(
        trend_source_subscription_id=uuid.uuid4(),
        run_key="test-run",
        status="running",
    )
    assert poll.run_key == "test-run"
