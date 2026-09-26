import pytest

from katcha.acquisition.adapters import get_adapter


def test_operator_feed_adapter_normalizes_operator_urls() -> None:
    adapter = get_adapter("operator_feed", "v1")

    batch = adapter.discover(
        {
            "feed_key": "ranksnaxx-short-drops",
            "default_platform": "tiktok",
            "default_content_kind": "shortform_clip",
            "default_metadata": {"content_lane": "viral_rank_clip"},
            "items": [
                {
                    "source_url": "https://www.tiktok.com/@creator/video/123",
                    "external_id": "tt-123",
                    "title": "Unexpected comeback",
                    "creator": "@creator",
                    "tags": ["comeback", "sports"],
                    "metrics": {"views": 1200000, "likes": 88000},
                    "captured_at": "2026-09-26T11:15:00Z",
                    "clip": {"start_seconds": 2, "end_seconds": 17},
                },
                {
                    "url": "https://www.instagram.com/reel/example/",
                    "platform": "instagram",
                    "metadata": {"content_lane": "ig_override"},
                },
                {
                    "source_url": "https://example.com/disabled",
                    "enabled": False,
                },
            ],
            "urls": ["https://clips.example.test/drop/1"],
        },
        {},
    )

    assert batch.done is True
    assert batch.provider_usage == {
        "operator_feed.items": 3,
        "operator_feed.skipped": 1,
    }
    assert [item.source_url for item in batch.items] == [
        "https://www.tiktok.com/@creator/video/123",
        "https://www.instagram.com/reel/example/",
        "https://clips.example.test/drop/1",
    ]
    first = batch.items[0]
    assert first.external_id == "tt-123"
    assert first.title == "Unexpected comeback"
    assert first.creator == "@creator"
    assert first.provenance_confidence == 0.65
    assert first.provenance_claims == {
        "discovered_via": "operator_feed",
        "operator_supplied": True,
        "feed_key": "ranksnaxx-short-drops",
        "platform": "tiktok",
    }
    assert first.metadata["operator_feed_key"] == "ranksnaxx-short-drops"
    assert first.metadata["operator_feed_rank"] == 1
    assert first.metadata["content_lane"] == "viral_rank_clip"
    assert first.metadata["platform_hint"] == "tiktok"
    assert first.metadata["content_kind"] == "shortform_clip"
    assert first.metadata["tags"] == ["comeback", "sports"]
    assert first.metadata["source_metrics"] == {"views": 1200000, "likes": 88000}
    assert first.metadata["clip"] == {"start_seconds": 2, "end_seconds": 17}

    assert batch.items[1].metadata["content_lane"] == "ig_override"
    assert batch.items[1].metadata["platform_hint"] == "instagram"
    assert batch.items[2].metadata["platform_hint"] == "tiktok"


def test_operator_feed_adapter_rejects_bad_shapes() -> None:
    adapter = get_adapter("operator_feed", "v1")

    with pytest.raises(ValueError, match="query.items must be a list"):
        adapter.discover({"items": {"source_url": "https://example.com"}}, {})

    with pytest.raises(ValueError, match="missing source_url"):
        adapter.discover({"items": [{"title": "missing"}]}, {})

    with pytest.raises(ValueError, match="item.metrics must be an object"):
        adapter.discover(
            {
                "items": [
                    {
                        "source_url": "https://example.com/video",
                        "metrics": ["bad"],
                    }
                ]
            },
            {},
        )


def test_operator_feed_adapter_is_registered() -> None:
    adapter = get_adapter("operator_feed", "v1")

    assert adapter.key == "operator_feed"
    assert adapter.version == "v1"
