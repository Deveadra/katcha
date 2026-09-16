from __future__ import annotations

from datetime import datetime, timedelta, timezone

from katcha.trends.scoring import SignalSample, TopicDescriptor, WatchConfig, score_topic

NOW = datetime(2026, 9, 16, 4, 0, tzinfo=timezone.utc)


def _sample(
    entity: str,
    source: str,
    independence: str,
    age_hours: float,
    views: float,
    comments: float,
) -> SignalSample:
    observed = NOW - timedelta(hours=age_hours)
    return SignalSample(
        entity_key=entity,
        source_kind=source,
        independence_key=independence,
        observed_at=observed,
        published_at=observed - timedelta(hours=1),
        metrics={"views": views, "comments": comments},
    )


def test_cross_source_acceleration_beats_flat_single_source() -> None:
    topic = TopicDescriptor("Project Nova", aliases=("Nova",), tags=("gaming", "xbox"))
    watch = WatchConfig(interests=("gaming", "xbox"))
    accelerating = [
        _sample("reddit:1", "reddit", "r/games", 20, 100, 20),
        _sample("reddit:1", "reddit", "r/games", 3, 2200, 450),
        _sample("youtube:1", "youtube", "official-studio", 4, 8000, 600),
        _sample("news:1", "news", "developer-site", 2, 700, 90),
    ]
    flat = [
        _sample("reddit:2", "reddit", "r/games", 20, 2000, 300),
        _sample("reddit:2", "reddit", "r/games", 3, 2100, 310),
    ]

    hot = score_topic(topic=topic, samples=accelerating, watch=watch, now=NOW)
    cold = score_topic(topic=topic, samples=flat, watch=watch, now=NOW)

    assert hot.opportunity_score > cold.opportunity_score
    assert hot.confidence > cold.confidence
    assert hot.components["source_breadth"] > cold.components["source_breadth"]


def test_single_source_spike_is_confidence_capped() -> None:
    score = score_topic(
        topic=TopicDescriptor("Project Nova", tags=("gaming",)),
        samples=[
            _sample("reddit:1", "reddit", "r/games", 18, 100, 10),
            _sample("reddit:1", "reddit", "r/games", 2, 500000, 10000),
        ],
        watch=WatchConfig(interests=("gaming",)),
        now=NOW,
    )

    assert score.confidence <= 0.55
    assert "single_independent_source_confidence_cap" in score.reasons


def test_declining_recent_rate_is_cooling() -> None:
    score = score_topic(
        topic=TopicDescriptor("Project Nova", tags=("gaming",)),
        samples=[
            _sample("reddit:1", "reddit", "r/games", 20, 100, 10),
            _sample("reddit:1", "reddit", "r/games", 10, 5000, 900),
            _sample("reddit:1", "reddit", "r/games", 2, 5100, 910),
        ],
        watch=WatchConfig(interests=("gaming",)),
        now=NOW,
    )

    assert score.lifecycle == "cooling"


def test_excluded_topic_is_not_an_opportunity() -> None:
    score = score_topic(
        topic=TopicDescriptor("Mobile gambling launch", tags=("gaming", "casino")),
        samples=[_sample("news:1", "news", "publisher", 1, 10000, 1000)],
        watch=WatchConfig(interests=("gaming",), excluded_terms=("gambling",)),
        now=NOW,
    )

    assert score.opportunity_score == 0.0
    assert "excluded_by_watch_profile" in score.reasons
