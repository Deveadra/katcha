import pytest

from katcha.acquisition.adapters import available_adapters, get_adapter


def test_manifest_adapter_is_metadata_only_and_deterministic() -> None:
    adapter = get_adapter("manifest", "v1")
    batch = adapter.discover(
        {
            "items": [
                {
                    "source_url": "https://example.com/video/1",
                    "external_id": "one",
                    "title": "Example",
                    "creator": "Creator",
                    "provenance_confidence": 0.8,
                    "metadata": {"velocity": 12},
                }
            ]
        },
        {},
    )

    assert batch.done is True
    assert batch.next_cursor == {}
    assert len(batch.items) == 1
    assert batch.items[0].source_url == "https://example.com/video/1"
    assert batch.items[0].external_id == "one"
    assert batch.items[0].metadata == {"velocity": 12}


def test_manifest_adapter_rejects_items_without_source_url() -> None:
    adapter = get_adapter("manifest", "v1")

    with pytest.raises(ValueError, match="missing source_url"):
        adapter.discover({"items": [{"external_id": "missing"}]}, {})


def test_unknown_adapter_fails_closed() -> None:
    with pytest.raises(ValueError, match="not installed"):
        get_adapter("unknown-provider", "v1")


def test_available_adapters_expose_operator_configuration_metadata() -> None:
    adapters = {
        (item["key"], item["version"]): item
        for item in available_adapters()
    }

    operator_feed = adapters[("operator_feed", "v1")]
    assert operator_feed["supports_imports"] is True
    assert "server_drop" in operator_feed["source_types"]
    assert "tiktok" in operator_feed["supported_platforms"]
    assert "instagram" in operator_feed["supported_platforms"]
    assert "items" in operator_feed["query_fields"]
    assert operator_feed["sample_query"]["feed_key"] == "ranksnaxx-short-drops"

    youtube = adapters[("youtube", "v1")]
    assert youtube["required_credentials"] == ["YOUTUBE_DATA_API_KEY"]
    assert "region_code" in youtube["query_fields"]
