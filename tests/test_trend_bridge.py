from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from katcha.acquisition_models import (
    DiscoveryCandidate,
    DiscoveryObservation,
    DiscoveryRun,
    TopicWatchVersion,
)
from katcha.api.main import app
from katcha.api.trend_bridge import _refresh_identity
from katcha.discovery_trend_models import TrendReviewQueueItem
from katcha.services.trend_bridge import (
    _completed_run,
    build_discovery_signal_spec,
    source_independence_key,
    topic_for_discovery,
)


def _candidate(
    *,
    adapter_key: str,
    source_url: str,
    external_id: str,
    title: str | None,
    creator: str | None = None,
    creator_url: str | None = None,
    provenance_claims: dict[str, object] | None = None,
) -> DiscoveryCandidate:
    return DiscoveryCandidate(
        id=uuid.uuid4(),
        adapter_key=adapter_key,
        external_id=external_id,
        source_url=source_url,
        canonical_url=source_url,
        platform=adapter_key,
        status="discovered",
        title=title,
        creator=creator,
        creator_url=creator_url,
        provenance_confidence=Decimal("0.9"),
        provenance_claims=provenance_claims or {},
        candidate_metadata={},
    )


def _observation(
    candidate: DiscoveryCandidate,
    metadata: dict[str, object],
) -> DiscoveryObservation:
    return DiscoveryObservation(
        id=uuid.uuid4(),
        discovery_run_id=uuid.uuid4(),
        discovery_candidate_id=candidate.id,
        external_id=candidate.external_id,
        observation_metadata=metadata,
        observed_at=datetime(2026, 9, 16, 10, 0, tzinfo=UTC),
    )


def _watch() -> TopicWatchVersion:
    return TopicWatchVersion(
        id=uuid.uuid4(),
        watch_key="gaming-xbox",
        version=3,
        name="Gaming / Xbox",
        enabled=True,
        include_terms=["gaming", "xbox"],
        exclude_terms=[],
        adapter_configs=[],
        language="en",
        locale="en-US",
        freshness_horizon_hours=72,
        max_candidates=100,
        watch_metadata={},
    )


def _run(*, status: str = "completed", query: dict[str, object] | None = None) -> DiscoveryRun:
    return DiscoveryRun(
        id=uuid.uuid4(),
        adapter_key="youtube",
        adapter_version="v1",
        run_key="run-1",
        status=status,
        query=query or {},
        cursor={},
        run_metadata={},
    )


def test_bridge_rejects_non_completed_discovery_runs() -> None:
    run = _run(status="running")

    with pytest.raises(ValueError, match="not completed"):
        _completed_run(run)


def test_youtube_mapping_preserves_metrics_and_channel_independence() -> None:
    candidate = _candidate(
        adapter_key="youtube",
        source_url="https://www.youtube.com/watch?v=abc123",
        external_id="abc123",
        title="Project Nova gameplay trailer",
        creator="Studio Channel",
        creator_url="https://www.youtube.com/channel/UC123",
        provenance_claims={"channel_id": "UC123"},
    )
    observation = _observation(
        candidate,
        {
            "channel_id": "UC123",
            "published_at": "2026-09-16T08:00:00Z",
            "description": "First gameplay reveal.",
            "source_metrics": {"views": 25000, "likes": 1400, "comments": 88},
        },
    )

    spec = build_discovery_signal_spec(candidate, observation, watch=_watch())

    assert spec.independence_key == "youtube-channel:uc123"
    assert spec.metrics == {"views": 25000.0, "likes": 1400.0, "comments": 88.0}
    assert spec.published_at == datetime(2026, 9, 16, 8, 0, tzinfo=UTC)
    assert spec.body_excerpt == "First gameplay reveal."
    assert spec.metadata["discovery_candidate_id"] == str(candidate.id)
    assert spec.metadata["rights_inferred"] is False
    assert spec.media_refs[0]["reuse_permission"] == "not_inferred"
    assert spec.match_confidence == 0.75


def test_reddit_mapping_uses_community_as_conservative_independence_boundary() -> None:
    candidate = _candidate(
        adapter_key="reddit",
        source_url="https://www.reddit.com/r/gaming/comments/abc/example/",
        external_id="t3_abc",
        title="Project Nova trailer is everywhere",
        creator="nova_fan",
    )
    observation = _observation(
        candidate,
        {
            "subreddit": "gaming",
            "published_at": "2026-09-16T09:00:00Z",
            "outbound_url": "https://example.com/trailer",
            "source_metrics": {"upvotes": 500, "comments": 72, "score": 450},
        },
    )

    spec = build_discovery_signal_spec(candidate, observation)

    assert source_independence_key(candidate, observation.observation_metadata) == (
        "reddit-subreddit:gaming"
    )
    assert spec.community == "gaming"
    assert spec.media_refs[0]["outbound_url"] == "https://example.com/trailer"
    assert spec.metrics["upvotes"] == 500.0


def test_rss_mapping_uses_feed_identity_and_summary() -> None:
    candidate = _candidate(
        adapter_key="rss_atom",
        source_url="https://studio.example/news/nova",
        external_id="nova-news",
        title="Studio explains Project Nova mechanics",
        creator="Studio Newsroom",
        provenance_claims={"feed_url": "https://studio.example/feed.xml"},
    )
    observation = _observation(
        candidate,
        {
            "feed_url": "https://studio.example/feed.xml",
            "feed_title": "Studio Developer News",
            "summary": "Combat, traversal and launch details.",
            "published_at": "2026-09-16T07:30:00+00:00",
            "source_metrics": {"feed_rank": 1},
        },
    )

    spec = build_discovery_signal_spec(candidate, observation)

    assert spec.independence_key == "feed:https://studio.example/feed.xml"
    assert spec.source_name == "Studio Developer News"
    assert spec.body_excerpt == "Combat, traversal and launch details."


def test_queue_cluster_label_becomes_shared_topic_with_stronger_confidence() -> None:
    candidate = _candidate(
        adapter_key="youtube",
        source_url="https://www.youtube.com/watch?v=abc123",
        external_id="abc123",
        title="A differently worded trailer title",
    )
    observation = _observation(candidate, {"source_metrics": {"views": 1000}})
    watch = _watch()
    queue = TrendReviewQueueItem(
        id=uuid.uuid4(),
        topic_watch_id=watch.id,
        discovery_candidate_id=candidate.id,
        trend_score_id=uuid.uuid4(),
        queue_key="execution-1",
        rank=1,
        status="pending",
        queue_metadata={
            "cluster_key": "project-nova",
            "cluster_label": "Project Nova gameplay reveal",
            "cluster_member_ids": [str(candidate.id)],
            "cluster_source_keys": ["youtube", "reddit"],
            "cluster_shared_tokens": ["project", "nova", "gameplay"],
        },
    )

    spec = build_discovery_signal_spec(
        candidate,
        observation,
        queue_item=queue,
        watch=watch,
    )

    assert topic_for_discovery(candidate, queue_metadata=queue.queue_metadata, watch=watch) == (
        "Project Nova gameplay reveal"
    )
    assert spec.topic == "Project Nova gameplay reveal"
    assert spec.aliases == ["project", "nova", "gameplay"]
    assert spec.metadata["cluster_key"] == "project-nova"
    assert spec.metadata["queue_key"] == "execution-1"
    assert spec.match_confidence == 0.95
    assert spec.match_reasons == ["discovery_queue_cluster"]


def test_unclustered_topic_fallback_uses_run_query_and_stable_observation_key() -> None:
    candidate = _candidate(
        adapter_key="youtube",
        source_url="https://www.youtube.com/watch?v=abc123",
        external_id="abc123",
        title=None,
    )
    observation = _observation(candidate, {"source_metrics": {"views": 1000}})
    run = _run(query={"q": "Project Nova gameplay"})

    first = build_discovery_signal_spec(candidate, observation, run=run)
    second = build_discovery_signal_spec(candidate, observation, run=run)

    assert first.topic == "Project Nova gameplay"
    assert first.observation_key == f"discovery-observation:{observation.id}"
    assert second.observation_key == first.observation_key
    assert second.independence_key == first.independence_key
    assert first.match_confidence == 0.75
    assert first.match_reasons == ["discovery_deterministic_fallback"]


def test_refresh_identity_is_stable_and_channel_scoped() -> None:
    first_channel = uuid.uuid4()
    second_channel = uuid.uuid4()

    first = _refresh_identity("queue:watch:run", first_channel)
    repeated = _refresh_identity("queue:watch:run", first_channel)
    other = _refresh_identity("queue:watch:run", second_channel)

    assert first == repeated
    assert first != other
    assert first[0].startswith("discovery-bridge-")
    assert first[1].startswith(f"channel-trend-refresh-{first_channel}-")


def test_trend_bridge_routes_include_durable_signal_inspection() -> None:
    paths = set(app.openapi()["paths"])

    assert "/v1/trends/bridge/runs/{discovery_run_id}" in paths
    assert "/v1/trends/bridge/watches/{topic_watch_id}/queues/{queue_key}" in paths
    assert "/v1/trends/bridge/signals" in paths
