from katcha.acquisition.adapters import get_adapter
from katcha.acquisition.feeds import parse_feed


def test_rss_feed_parses_publication_metadata_and_terms() -> None:
    xml = b"""<?xml version="1.0"?>
    <rss version="2.0">
      <channel>
        <title>Game News</title>
        <item>
          <guid>xbox-one</guid>
          <title>New Xbox game trailer explodes overnight</title>
          <link>https://example.com/xbox-one</link>
          <pubDate>Wed, 16 Sep 2026 01:00:00 GMT</pubDate>
          <author>Studio Newsroom</author>
          <description>Gameplay reveal and launch details.</description>
        </item>
        <item>
          <guid>other</guid>
          <title>Unrelated cooking story</title>
          <link>https://example.com/cooking</link>
          <description>Recipe details.</description>
        </item>
      </channel>
    </rss>
    """

    items = parse_feed(
        xml,
        feed_url="https://example.com/feed.xml",
        include_terms=["xbox"],
        limit=10,
    )

    assert len(items) == 1
    item = items[0]
    assert item.external_id == "xbox-one"
    assert item.source_url == "https://example.com/xbox-one"
    assert item.creator == "Studio Newsroom"
    assert item.metadata["published_at"] == "2026-09-16T01:00:00+00:00"
    assert item.metadata["source_metrics"] == {"feed_rank": 1}


def test_atom_feed_parses_alternate_link() -> None:
    xml = b"""<?xml version="1.0" encoding="utf-8"?>
    <feed xmlns="http://www.w3.org/2005/Atom">
      <title>Developer Updates</title>
      <entry>
        <id>tag:example.com,2026:launch</id>
        <title>Project Nova mechanics revealed</title>
        <updated>2026-09-16T02:15:00Z</updated>
        <link rel="alternate" href="https://example.com/nova" />
        <summary>Combat and traversal systems.</summary>
      </entry>
    </feed>
    """

    items = parse_feed(xml, feed_url="https://example.com/atom.xml")

    assert len(items) == 1
    assert items[0].external_id == "tag:example.com,2026:launch"
    assert items[0].source_url == "https://example.com/nova"
    assert items[0].metadata["feed_title"] == "Developer Updates"


def test_rss_atom_adapter_is_registered() -> None:
    adapter = get_adapter("rss_atom", "v1")

    assert adapter.key == "rss_atom"
    assert adapter.version == "v1"
