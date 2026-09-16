from __future__ import annotations

from datetime import UTC, datetime

from katcha.acquisition.adapters import available_adapters, get_adapter
from katcha.acquisition.reddit_discovery import parse_reddit_candidates


def test_reddit_adapter_is_registered() -> None:
    assert {item["key"] for item in available_adapters()} >= {
        "manifest",
        "reddit",
        "rss_atom",
        "youtube",
    }
    assert get_adapter("reddit", "v1").version == "v1"


def test_parse_reddit_candidates_preserves_metrics_and_provenance() -> None:
    now = datetime(2026, 9, 16, 4, 0, tzinfo=UTC)
    payload = {
        "data": {
            "after": "t3_next",
            "children": [
                {
                    "kind": "t3",
                    "data": {
                        "name": "t3_abc",
                        "id": "abc",
                        "title": "New game trailer is everywhere",
                        "author": "creator",
                        "subreddit": "gaming",
                        "permalink": "/r/gaming/comments/abc/example/",
                        "url_overridden_by_dest": "https://example.com/trailer",
                        "domain": "example.com",
                        "post_hint": "hosted:video",
                        "is_video": True,
                        "over_18": False,
                        "created_utc": now.timestamp() - 1800,
                        "score": 450,
                        "ups": 500,
                        "num_comments": 72,
                        "upvote_ratio": 0.94,
                    },
                }
            ],
        }
    }

    candidates = parse_reddit_candidates(
        payload,
        freshness_horizon_hours=24,
        now=now,
    )

    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.external_id == "t3_abc"
    assert candidate.creator == "creator"
    assert candidate.provenance_confidence == 0.9
    assert candidate.source_url.endswith("/r/gaming/comments/abc/example/")
    assert candidate.metadata["subreddit"] == "gaming"
    assert candidate.metadata["source_metrics"] == {
        "score": 450,
        "upvotes": 500,
        "comments": 72,
        "search_rank": 1,
    }


def test_parse_reddit_candidates_drops_stale_posts() -> None:
    now = datetime(2026, 9, 16, 4, 0, tzinfo=UTC)
    payload = {
        "data": {
            "children": [
                {
                    "kind": "t3",
                    "data": {
                        "name": "t3_old",
                        "id": "old",
                        "title": "Old post",
                        "author": "creator",
                        "permalink": "/r/gaming/comments/old/example/",
                        "created_utc": now.timestamp() - (72 * 3600),
                    },
                }
            ]
        }
    }

    candidates = parse_reddit_candidates(
        payload,
        freshness_horizon_hours=24,
        now=now,
    )

    assert candidates == ()
