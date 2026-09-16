from __future__ import annotations

import httpx
import pytest

from katcha.acquisition.adapters import available_adapters, get_adapter
from katcha.acquisition.youtube_discovery import (
    _provider_get_json,
    parse_youtube_candidates,
)


def test_youtube_adapter_is_registered() -> None:
    assert {item["key"] for item in available_adapters()} >= {
        "manifest",
        "rss_atom",
        "youtube",
    }
    assert get_adapter("youtube", "v1").version == "v1"


def test_parse_youtube_candidates_preserves_metrics_and_provenance() -> None:
    search_payload = {
        "items": [
            {
                "id": {"videoId": "abc123"},
                "snippet": {
                    "title": "Search title",
                    "channelTitle": "Search channel",
                    "publishedAt": "2026-09-16T01:00:00Z",
                },
            }
        ],
        "nextPageToken": "NEXT",
    }
    videos_payload = {
        "items": [
            {
                "id": "abc123",
                "snippet": {
                    "title": "Hydrated title",
                    "description": "Description",
                    "channelTitle": "Creator",
                    "channelId": "UC123",
                    "publishedAt": "2026-09-16T01:00:00Z",
                },
                "statistics": {
                    "viewCount": "12500",
                    "likeCount": "900",
                    "commentCount": "44",
                },
            }
        ]
    }

    candidates = parse_youtube_candidates(search_payload, videos_payload)

    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.external_id == "abc123"
    assert candidate.source_url == "https://www.youtube.com/watch?v=abc123"
    assert candidate.title == "Hydrated title"
    assert candidate.creator == "Creator"
    assert candidate.provenance_confidence == 0.95
    assert candidate.metadata["published_at"] == "2026-09-16T01:00:00Z"
    assert candidate.metadata["source_metrics"] == {
        "views": 12500,
        "likes": 900,
        "comments": 44,
        "search_rank": 1,
    }


def test_youtube_provider_error_does_not_expose_api_key() -> None:
    api_key = "secret-youtube-key"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, request=request, json={"error": "forbidden"})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(RuntimeError) as exc_info:
            _provider_get_json(
                client,
                "https://www.googleapis.com/youtube/v3/search",
                params={"key": api_key, "part": "snippet"},
                operation="search",
            )

    assert api_key not in str(exc_info.value)
    assert "status 403" in str(exc_info.value)
