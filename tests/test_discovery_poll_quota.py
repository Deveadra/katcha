from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import UniqueConstraint

from katcha.api.main import app
from katcha.discovery_poll_models import (
    DiscoveryCollectionClaim,
    DiscoveryProviderQuotaWindow,
    DiscoveryQuotaReservation,
    TrendWatchPollAttempt,
)
from katcha.services.discovery_polling import (
    collection_window_key,
    deterministic_backoff_seconds,
    provider_page_demands,
    source_identity,
)


def _unique_columns(table) -> set[tuple[str, ...]]:
    return {
        tuple(column.name for column in constraint.columns)
        for constraint in table.constraints
        if isinstance(constraint, UniqueConstraint)
    }


def test_poll_attempt_and_collection_claim_idempotency_constraints() -> None:
    assert ("source_state_id", "execution_key") in _unique_columns(
        TrendWatchPollAttempt.__table__
    )
    assert ("source_identity", "collection_window_key") in _unique_columns(
        DiscoveryCollectionClaim.__table__
    )
    assert ("provider_key", "bucket_key", "window_key") in _unique_columns(
        DiscoveryProviderQuotaWindow.__table__
    )
    assert ("poll_attempt_id", "page_key", "bucket_key") in _unique_columns(
        DiscoveryQuotaReservation.__table__
    )


def test_source_identity_is_stable_and_query_sensitive() -> None:
    first = source_identity("youtube", "v1", {"q": "xbox", "limit": 25})
    same = source_identity("youtube", "v1", {"limit": 25, "q": "xbox"})
    changed = source_identity("youtube", "v1", {"q": "playstation", "limit": 25})
    assert first == same
    assert first != changed


def test_collection_window_is_deterministic() -> None:
    first = collection_window_key(
        datetime(2026, 9, 24, 6, 0, 1, tzinfo=UTC),
        window_seconds=300,
    )
    same = collection_window_key(
        datetime(2026, 9, 24, 6, 4, 59, tzinfo=UTC),
        window_seconds=300,
    )
    next_window = collection_window_key(
        datetime(2026, 9, 24, 6, 5, 0, tzinfo=UTC),
        window_seconds=300,
    )
    assert first == same
    assert first != next_window


def test_youtube_reserves_search_and_hydration_as_separate_buckets() -> None:
    demands = provider_page_demands(
        "youtube",
        {},
        now=datetime(2026, 9, 24, 6, 0, tzinfo=UTC),
    )
    by_bucket = {item.bucket_key: item for item in demands}
    assert set(by_bucket) == {"youtube.search.list", "youtube.core"}
    assert by_bucket["youtube.search.list"].units == 1
    assert by_bucket["youtube.search.list"].limit_units == 100
    assert by_bucket["youtube.core"].units == 1
    assert by_bucket["youtube.core"].limit_units == 10000
    assert by_bucket["youtube.search.list"].window_key.endswith(
        ":America/Los_Angeles"
    )


def test_youtube_daily_quota_rolls_at_pacific_midnight() -> None:
    before = provider_page_demands(
        "youtube",
        {},
        now=datetime(2026, 9, 24, 6, 59, 59, tzinfo=UTC),
    )
    after = provider_page_demands(
        "youtube",
        {},
        now=datetime(2026, 9, 24, 7, 0, 0, tzinfo=UTC),
    )
    before_search = next(item for item in before if item.bucket_key == "youtube.search.list")
    after_search = next(item for item in after if item.bucket_key == "youtube.search.list")
    assert before_search.window_key != after_search.window_key
    assert before_search.window_end == after_search.window_start


def test_reddit_api_and_oauth_caps_are_independent() -> None:
    demands = provider_page_demands(
        "reddit",
        {
            "provider_quota_limits": {
                "reddit.api": {"limit": 50, "window_seconds": 600},
                "reddit.oauth": {"limit": 10, "window_seconds": 3600},
            }
        },
        now=datetime(2026, 9, 24, 6, 1, tzinfo=UTC),
    )
    by_bucket = {item.bucket_key: item for item in demands}
    assert set(by_bucket) == {"reddit.api", "reddit.oauth"}
    assert by_bucket["reddit.api"].limit_units == 50
    assert by_bucket["reddit.oauth"].limit_units == 10
    assert by_bucket["reddit.api"].window_end != by_bucket["reddit.oauth"].window_end


def test_provider_quota_override_can_define_short_window() -> None:
    demands = provider_page_demands(
        "reddit",
        {
            "provider_quota_limits": {
                "reddit.api": {"limit": 50, "window_seconds": 600}
            }
        },
        now=datetime(2026, 9, 24, 6, 1, tzinfo=UTC),
    )
    assert len(demands) == 1
    assert demands[0].bucket_key == "reddit.api"
    assert demands[0].limit_units == 50
    assert demands[0].units == 1
    assert demands[0].window_end > demands[0].window_start


def test_backoff_is_deterministic_and_honors_retry_after() -> None:
    first = deterministic_backoff_seconds("source-a", 3)
    same = deterministic_backoff_seconds("source-a", 3)
    assert first == same
    assert deterministic_backoff_seconds(
        "source-a",
        3,
        retry_after_seconds=7200,
    ) >= 7200


def test_poll_history_quota_and_reset_routes_are_mounted() -> None:
    paths = set(app.openapi()["paths"])
    assert "/v1/trends/watches/{topic_watch_id}/poll-attempts" in paths
    assert (
        "/v1/trends/watches/{topic_watch_id}/sources/{adapter_index}/quota"
        in paths
    )
    assert (
        "/v1/trends/watches/{topic_watch_id}/sources/{adapter_index}/reset"
        in paths
    )
