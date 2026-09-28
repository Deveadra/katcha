from katcha.acquisition.adapters import available_adapters
from katcha.acquisition.web_scout import parse_web_scout_output


def test_web_scout_adapter_is_registered() -> None:
    catalog = {(row["key"], row["version"]): row for row in available_adapters()}

    assert ("web_scout", "v1") in catalog
    assert "tiktok" in catalog[("web_scout", "v1")]["supported_platforms"]
    assert "bluesky" in catalog[("web_scout", "v1")]["supported_platforms"]


def test_web_scout_parser_only_accepts_grounded_urls() -> None:
    items = parse_web_scout_output(
        """
        {
          "items": [
            {
              "source_url": "https://www.tiktok.com/@newcreator/video/123",
              "title": "Fresh clip",
              "creator": "@newcreator",
              "why_relevant": "Fast-growing gaming clip"
            },
            {
              "source_url": "https://invented.example.invalid/post/9",
              "title": "Hallucinated"
            }
          ]
        }
        """,
        grounded_urls={"https://tiktok.com/@newcreator/video/123"},
        limit=10,
    )

    assert len(items) == 1
    assert items[0].source_url == "https://tiktok.com/@newcreator/video/123"
    assert items[0].metadata["platform"] == "tiktok"
    assert items[0].provenance_claims["grounded_search_result"] is True
