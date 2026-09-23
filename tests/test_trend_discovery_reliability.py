from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import CheckConstraint, UniqueConstraint

from katcha.acquisition.adapters import DiscoveryProviderError
from katcha.acquisition_models import TopicWatchVersion
from katcha.api.main import app
from katcha.discovery_trend_models import TrendWatchSourceState
from katcha.services.trend_signal_bridge import _float_metrics, _observation_key
from katcha.services.trend_source_reliability import (
    _classify_failure,
    source_health_allows_refresh,
    watch_scope_key,
)


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


def test_topic_watch_versions_are_scoped_by_channel() -> None:
    table = TopicWatchVersion.__table__
    assert ("scope_key", "watch_key", "version") in _unique_columns(table)
    assert table.c.channel_profile_id.nullable is True
    assert table.c.scope_key.nullable is False
    channel_id = uuid.uuid4()
    assert watch_scope_key(channel_id) == f"channel:{channel_id}"
    assert watch_scope_key(None) == "global"


def test_source_state_persists_health_cursor_and_backoff() -> None:
    table = TrendWatchSourceState.__table__
    assert ("topic_watch_id", "adapter_index") in _unique_columns(table)
    checks = _check_names(table)
    assert "ck_trend_watch_source_adapter_index_nonnegative" in checks
    assert "ck_trend_watch_source_failures_nonnegative" in checks
    assert table.c.cursor.nullable is False
    assert table.c.backoff_until.nullable is True
    assert table.c.last_discovery_run_id.nullable is True


def test_provider_error_retains_retry_after_for_health_classifier() -> None:
    error = DiscoveryProviderError(
        "Reddit search request failed",
        kind="rate_limited",
        status_code=429,
        retry_after_seconds=120,
    )
    kind, transient, retry_after = _classify_failure(str(error))
    assert kind == "rate_limited"
    assert transient is True
    assert retry_after == 120


def test_provider_5xx_is_transient_and_source_coverage_can_withhold() -> None:
    kind, transient, retry_after = _classify_failure(
        "YouTube search request failed with status 503"
    )
    assert kind == "provider_unavailable"
    assert transient is True
    assert retry_after is None
    assert source_health_allows_refresh(
        {"configured": 4, "coverage": 0.75},
        minimum_coverage=0.75,
    )
    assert not source_health_allows_refresh(
        {"configured": 4, "coverage": 0.5},
        minimum_coverage=0.75,
    )


def test_rss_observation_contributes_mentions_signal() -> None:
    metrics = _float_metrics({"source_metrics": {"feed_rank": 1}}, "rss_atom")
    assert metrics == {"mentions": 1.0}


def test_bridge_observation_key_deduplicates_equivalent_channel_snapshots() -> None:
    now = datetime(2026, 9, 16, 10, 1, tzinfo=UTC)
    first = _observation_key(
        provider_key="youtube",
        external_id="video-123",
        observed_at=now,
        metrics={"views": 1000.0, "likes": 100.0},
        canonical_url="https://www.youtube.com/watch?v=video-123",
    )
    second = _observation_key(
        provider_key="youtube",
        external_id="video-123",
        observed_at=now + timedelta(minutes=2),
        metrics={"views": 1000.0, "likes": 100.0},
        canonical_url="https://www.youtube.com/watch?v=video-123",
    )
    changed = _observation_key(
        provider_key="youtube",
        external_id="video-123",
        observed_at=now + timedelta(minutes=2),
        metrics={"views": 1100.0, "likes": 105.0},
        canonical_url="https://www.youtube.com/watch?v=video-123",
    )
    assert first == second
    assert first != changed


def test_reliability_routes_are_mounted() -> None:
    paths = set(app.openapi()["paths"])
    assert "/v1/channels/{channel_profile_id}/trends/watches" in paths
    assert "/v1/trends/watches/{topic_watch_id}/health" in paths
    assert "/v1/channels/{channel_profile_id}/trends/source-health" in paths
