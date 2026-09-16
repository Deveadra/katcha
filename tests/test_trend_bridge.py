from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal

from katcha.acquisition_models import (
    DiscoveryCandidate,
    DiscoveryObservation,
    TopicWatchVersion,
)
from katcha.api.main import app
from katcha.discovery_trend_models import TrendReviewQueueItem
from katcha.services import trend_bridge
from katcha.services.trend_bridge import (
    build_discovery_signal_spec,
    source_independence_key,
    topic_for_discovery,
)


def _candidate(
    *,
    adapter_key: str,
    source_url: str,
    external_id: str,
    title: str,
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
    assert spec.media_refs[0]["source_url"] == candidate.source_url


def test_reddit_mapping_uses_author_before_community_for_independence() -> None:
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
        "reddit-user:nova_fan"
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


def test_queue_cluster_label_becomes_shared_topic_and_preserves_lineage() -> None:
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
    assert spec.match_reasons == ["discovery_queue_cluster"]


def test_unclustered_mapping_is_deterministic_and_observation_idempotent() -> None:
    candidate = _candidate(
        adapter_key="youtube",
        source_url="https://www.youtube.com/watch?v=abc123",
        external_id="abc123",
        title="Project Nova gameplay trailer",
    )
    observation = _observation(candidate, {"source_metrics": {"views": 1000}})

    first = build_discovery_signal_spec(candidate, observation)
    second = build_discovery_signal_spec(candidate, observation)

    assert first.topic == candidate.title
    assert first.observation_key == f"discovery-observation:{observation.id}"
    assert second.observation_key == first.observation_key
    assert second.independence_key == first.independence_key


def test_channel_refresh_run_key_is_stable_for_same_scope(monkeypatch) -> None:
    channel_id = uuid.uuid4()
    seen: list[tuple[uuid.UUID, str]] = []

    def fake_refresh(profile_id: uuid.UUID, *, run_key: str, now=None):
        del now
        seen.append((profile_id, run_key))
        return {}

    monkeypatch.setattr(trend_bridge, "refresh_channel_trends", fake_refresh)

    first = trend_bridge._refresh_channels([channel_id], scope="queue:watch:run")
    second = trend_bridge._refresh_channels([channel_id], scope="queue:watch:run")

    assert first == (channel_id,)
    assert second == (channel_id,)
    assert seen[0][1] == seen[1][1]


def test_trend_bridge_routes_are_mounted() -> None:
    paths = set(app.openapi()["paths"])

    assert "/v1/trends/bridge/runs/{discovery_run_id}" in paths
    assert "/v1/trends/bridge/watches/{topic_watch_id}/queues/{queue_key}" in paths
