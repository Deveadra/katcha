from datetime import UTC, datetime, timedelta

from katcha.acquisition.trends import TrendObservation, score_trend


def _observation(
    *,
    now: datetime,
    observed_hours_ago: float,
    published_hours_ago: float,
    views: int,
    source: str = "youtube",
) -> TrendObservation:
    return TrendObservation(
        observed_at=now - timedelta(hours=observed_hours_ago),
        published_at=now - timedelta(hours=published_hours_ago),
        source_key=source,
        metrics={"views": views},
    )


def test_fast_recent_growth_beats_stale_flat_history() -> None:
    now = datetime(2026, 9, 16, 3, 0, tzinfo=UTC)
    emerging = score_trend(
        [
            _observation(
                now=now,
                observed_hours_ago=3,
                published_hours_ago=4,
                views=1_000,
            ),
            _observation(
                now=now,
                observed_hours_ago=1,
                published_hours_ago=4,
                views=15_000,
            ),
            _observation(
                now=now,
                observed_hours_ago=0,
                published_hours_ago=4,
                views=30_000,
            ),
        ],
        now=now,
    )
    stale = score_trend(
        [
            _observation(
                now=now,
                observed_hours_ago=48,
                published_hours_ago=120,
                views=500_000,
            ),
            _observation(
                now=now,
                observed_hours_ago=0,
                published_hours_ago=120,
                views=501_000,
            ),
        ],
        now=now,
    )

    assert emerging.score > stale.score
    assert emerging.velocity > stale.velocity
    assert emerging.freshness > stale.freshness
    assert emerging.acceleration > 0


def test_cross_source_corroboration_increases_score() -> None:
    now = datetime(2026, 9, 16, 3, 0, tzinfo=UTC)
    history = [
        _observation(
            now=now,
            observed_hours_ago=1,
            published_hours_ago=2,
            views=10_000,
        ),
        _observation(
            now=now,
            observed_hours_ago=0,
            published_hours_ago=2,
            views=20_000,
        ),
    ]

    one_source = score_trend(history, now=now, corroborating_source_count=1)
    three_sources = score_trend(history, now=now, corroborating_source_count=3)

    assert three_sources.corroboration > one_source.corroboration
    assert three_sources.score > one_source.score


def test_saturation_penalizes_crowded_topic() -> None:
    now = datetime(2026, 9, 16, 3, 0, tzinfo=UTC)
    history = [
        _observation(
            now=now,
            observed_hours_ago=1,
            published_hours_ago=2,
            views=10_000,
        ),
        _observation(
            now=now,
            observed_hours_ago=0,
            published_hours_ago=2,
            views=25_000,
        ),
    ]

    novel = score_trend(history, now=now, related_item_count=0)
    saturated = score_trend(history, now=now, related_item_count=12)

    assert novel.novelty > saturated.novelty
    assert saturated.saturation_penalty > novel.saturation_penalty
    assert novel.score > saturated.score


def test_empty_history_is_bounded_and_inspectable() -> None:
    now = datetime(2026, 9, 16, 3, 0, tzinfo=UTC)
    result = score_trend([], now=now)

    assert 0 <= result.score <= 1
    assert result.observation_count == 0
    assert result.window_start is None
    assert result.window_end is None
    assert result.breakdown()["algorithm_version"] == "emerging-trend-v1"
