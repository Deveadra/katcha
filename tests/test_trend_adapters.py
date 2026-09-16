from __future__ import annotations

import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from katcha.config import Settings
from katcha.services.trend_source_health import source_health_allows_refresh
from katcha.services.trend_sources import _observation_key
from katcha.trends.adapters import (
    RedditTrendAdapter,
    RssTrendAdapter,
    TrendAdapterContext,
    TrendAdapterError,
    TrendObservation,
    YouTubeTrendAdapter,
)
from katcha.trends.scoring import SignalSample, TopicDescriptor, WatchConfig, score_topic

NOW = datetime(2026, 9, 16, 10, 0, tzinfo=UTC)


def _context(*, youtube: bool = False) -> TrendAdapterContext:
    return TrendAdapterContext(
        channel_profile_id=uuid.uuid4(),
        youtube_connection_id=uuid.uuid4() if youtube else None,
        observed_at=NOW,
        settings=Settings(),
    )


def test_reddit_adapter_preserves_engagement_and_media_refs() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        payload = {
            "data": {
                "after": "t3_next",
                "children": [
                    {
                        "data": {
                            "name": "t3_abc",
                            "title": "Project Nova gameplay reveal",
                            "selftext": "New mechanics are being discussed.",
                            "author": "player_one",
                            "subreddit": "gaming",
                            "subreddit_name_prefixed": "r/gaming",
                            "permalink": "/r/gaming/comments/abc/nova/",
                            "created_utc": NOW.timestamp() - 600,
                            "score": 1200,
                            "ups": 1275,
                            "num_comments": 312,
                            "upvote_ratio": 0.94,
                            "media": {
                                "reddit_video": {
                                    "fallback_url": "https://v.redd.it/abc/DASH_720.mp4"
                                }
                            },
                        }
                    }
                ],
            }
        }
        return httpx.Response(
            200,
            request=request,
            json=payload,
            headers={"x-ratelimit-remaining": "88"},
        )

    adapter = RedditTrendAdapter(httpx.MockTransport(handler))
    batch = adapter.poll(
        {"query": "Project Nova", "topic": "Project Nova", "subreddit": "gaming"},
        {},
        _context(),
    )

    assert batch.next_cursor["after"] == "t3_next"
    assert len(batch.observations) == 1
    observation = batch.observations[0]
    assert observation.metrics == {
        "score": 1200.0,
        "upvotes": 1275.0,
        "comments": 312.0,
    }
    assert observation.independence_key == "reddit:r/gaming"
    assert observation.media_refs[0]["rights_status"] == "unassessed"


def test_reddit_rate_limit_preserves_retry_after() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            429,
            request=request,
            headers={"Retry-After": "120"},
        )

    adapter = RedditTrendAdapter(httpx.MockTransport(handler))
    with pytest.raises(TrendAdapterError) as exc_info:
        adapter.poll({"query": "xbox"}, {}, _context())

    assert exc_info.value.kind == "rate_limited"
    assert exc_info.value.retry_after_seconds == 120


def test_rss_adapter_preserves_etag_mentions_and_unassessed_media() -> None:
    xml = b"""<?xml version="1.0"?>
    <rss version="2.0">
      <channel>
        <title>Studio News</title>
        <item>
          <guid>nova-launch</guid>
          <title>Project Nova launches next month</title>
          <link>https://studio.example/news/nova</link>
          <pubDate>Wed, 16 Sep 2026 09:30:00 GMT</pubDate>
          <description>Developer details and launch notes.</description>
          <enclosure url="https://studio.example/media/trailer.mp4" type="video/mp4" />
        </item>
      </channel>
    </rss>"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            request=request,
            content=xml,
            headers={
                "ETag": '"nova-v1"',
                "Last-Modified": "Wed, 16 Sep 2026 09:31:00 GMT",
            },
        )

    adapter = RssTrendAdapter(httpx.MockTransport(handler))
    batch = adapter.poll(
        {
            "feed_url": "https://studio.example/feed.xml",
            "topic": "Project Nova",
            "source_kind": "developer_news",
        },
        {},
        _context(),
    )

    assert batch.next_cursor["etag"] == '"nova-v1"'
    assert len(batch.observations) == 1
    observation = batch.observations[0]
    assert observation.metrics == {"mentions": 1.0}
    assert observation.source_kind == "developer_news"
    assert observation.media_refs[0]["rights_status"] == "unassessed"


def test_rss_adapter_uses_conditional_get_cursor() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["If-None-Match"] == '"nova-v1"'
        return httpx.Response(304, request=request)

    adapter = RssTrendAdapter(httpx.MockTransport(handler))
    batch = adapter.poll(
        {"feed_url": "https://studio.example/feed.xml", "topic": "Project Nova"},
        {"etag": '"nova-v1"'},
        _context(),
    )

    assert batch.observations == ()
    assert batch.metadata["not_modified"] is True


class _FakeYouTubeClient:
    def search_videos(self, query: str, **kwargs: object) -> dict[str, object]:
        assert query == "Project Nova trailer"
        assert kwargs["max_results"] == 10
        return {"items": [{"id": {"videoId": "vid123"}}]}

    def video_resources(self, video_ids: list[str]) -> list[dict[str, object]]:
        assert video_ids == ["vid123"]
        return [
            {
                "id": "vid123",
                "snippet": {
                    "title": "Project Nova Official Trailer",
                    "description": "First look at the game.",
                    "channelId": "studio-channel",
                    "channelTitle": "Nova Studio",
                    "publishedAt": "2026-09-16T08:00:00Z",
                    "tags": ["gaming", "xbox"],
                    "thumbnails": {
                        "high": {
                            "url": "https://img.example/nova.jpg",
                            "width": 480,
                            "height": 360,
                        }
                    },
                },
                "statistics": {
                    "viewCount": "50000",
                    "likeCount": "7000",
                    "commentCount": "1400",
                },
            }
        ]


def test_youtube_adapter_preserves_cumulative_counters() -> None:
    adapter = YouTubeTrendAdapter(
        client_factory=lambda _connection_id, _settings: _FakeYouTubeClient()
    )
    batch = adapter.poll(
        {
            "query": "Project Nova trailer",
            "topic": "Project Nova",
            "limit": 10,
        },
        {},
        _context(youtube=True),
    )

    assert len(batch.observations) == 1
    observation = batch.observations[0]
    assert observation.metrics == {
        "views": 50000.0,
        "likes": 7000.0,
        "comments": 1400.0,
    }
    assert observation.independence_key == "youtube:studio-channel"
    assert all(ref["rights_status"] == "unassessed" for ref in observation.media_refs)


def test_observation_key_deduplicates_same_snapshot_within_time_bucket() -> None:
    observation = TrendObservation(
        topic="Project Nova",
        provider_key="youtube",
        external_id="vid123",
        source_kind="youtube",
        independence_key="youtube:studio-channel",
        canonical_url="https://www.youtube.com/watch?v=vid123",
        metrics={"views": 50000.0, "likes": 7000.0},
    )
    first = _observation_key(
        observation,
        NOW + timedelta(seconds=15),
        bucket_seconds=300,
    )
    second = _observation_key(
        observation,
        NOW + timedelta(minutes=4, seconds=45),
        bucket_seconds=300,
    )
    later = _observation_key(
        observation,
        NOW + timedelta(minutes=5, seconds=1),
        bucket_seconds=300,
    )
    changed = _observation_key(
        replace(
            observation,
            metrics={"views": 51000.0, "likes": 7100.0},
        ),
        NOW + timedelta(seconds=15),
        bucket_seconds=300,
    )

    assert first == second
    assert first != later
    assert first != changed


def test_source_health_gate_withholds_degraded_collection() -> None:
    assert source_health_allows_refresh(
        {"configured": 0, "coverage": 1.0},
        minimum_coverage=0.75,
    )
    assert source_health_allows_refresh(
        {"configured": 4, "coverage": 0.75},
        minimum_coverage=0.75,
    )
    assert not source_health_allows_refresh(
        {"configured": 4, "coverage": 0.5},
        minimum_coverage=0.75,
    )


def test_feed_mentions_contribute_to_trend_activity() -> None:
    result = score_topic(
        topic=TopicDescriptor("Project Nova", tags=("gaming",)),
        samples=(
            SignalSample(
                entity_key="rss:nova-launch",
                source_kind="developer_news",
                independence_key="rss:studio.example",
                observed_at=NOW - timedelta(hours=1),
                published_at=NOW - timedelta(hours=2),
                metrics={"mentions": 1.0},
            ),
        ),
        watch=WatchConfig(interests=("gaming", "Project Nova")),
        now=NOW,
    )

    assert result.opportunity_score > 0
    assert result.components["data_quality"] > 0
