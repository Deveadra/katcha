# Katcha ingestion sources

Katcha separates **where a candidate was found** from **how the candidate is later
used**. Discovery can be aggressive across TikTok, Instagram, YouTube, Reddit,
Twitch, X, RSS feeds, Discord/server drops, licensed feeds, or any future site.
Every source is normalized into the same `DiscoveryCandidate` pipeline.

## Source registry

Register each source through `POST /v1/discovery/sources`.

```json
{
  "source_key": "ranksnaxx-tiktok-fails",
  "name": "RankSnaxx TikTok Fails Watch",
  "adapter_key": "operator_feed",
  "adapter_version": "v1",
  "platform": "tiktok",
  "usage_mode": "operator_authorized",
  "query_template": {
    "feed_key": "ranksnaxx-tiktok-fails",
    "default_platform": "tiktok",
    "default_content_kind": "rank_clip",
    "items": []
  },
  "default_candidate_metadata": {
    "content_lane": "viral_clip",
    "channel_family": "ranksnaxx"
  },
  "poll_interval_minutes": 15,
  "source_metadata": {
    "notes": "Operator-fed TikTok URLs until a native TikTok adapter is installed."
  }
}
```

`adapter_key` controls how Katcha discovers candidates. Existing adapters include:

- `manifest@v1` for operator-supplied URLs from any site.
- `operator_feed@v1` for operator or server supplied shortform drops with
  platform hints, metrics, tags, clip selectors, and provenance metadata.
- `rss_atom@v1` for websites with feeds.
- `reddit@v1` for Reddit discovery.
- `youtube@v1` for YouTube search discovery.

Future adapters should implement `DiscoveryAdapter` and register themselves in
`katcha.acquisition.adapters`. The rest of the pipeline does not need to know
whether the source is TikTok, Instagram, Twitch, X, a private server, or a new
site that appears later.

## Usage modes

`usage_mode` is operational metadata, not a hardcoded rights decision:

- `discovery_only`: collect signal and context only.
- `candidate_review`: queue as a candidate that needs operator decisioning.
- `operator_authorized`: operator controls whether it can proceed.
- `render_allowed`: source is intended for direct render workflows.
- `blocked`: keep the source configured but prevent normal use.

The active acquisition policy still decides whether a candidate can be promoted
into a managed `SourceItem`. This keeps the pipeline honest while allowing the
operator to decide how aggressively to source content.

## Creating runs from a source

Use `POST /v1/discovery/sources/{source_id}/runs` to create a discovery run from
the source's saved query template.

```json
{
  "idempotency_key": "ranksnaxx-tiktok-fails-2026-09-26T10",
  "query_overrides": {
    "feed_key": "ranksnaxx-tiktok-fails",
    "default_platform": "tiktok",
    "items": [
      {
        "source_url": "https://www.tiktok.com/@creator/video/123",
        "external_id": "tiktok-123",
        "title": "Unexpected comeback",
        "tags": ["comeback", "sports"],
        "metrics": {
          "views": 1200000,
          "likes": 88000
        },
        "clip": {
          "start_seconds": 2,
          "end_seconds": 17
        }
      }
    ]
  }
}
```

The run records:

- `ingestion_source_id`
- `ingestion_source_key`
- `source_platform`
- `source_usage_mode`
- `default_candidate_metadata`

Those fields are copied into each discovered candidate unless an item overrides a
specific metadata value.

## Operator feed shape

`operator_feed@v1` is the quickest way to add a new site, Discord/server drop,
or manual TikTok/Instagram/Twitch/X watch list while a native adapter is being
built.

```json
{
  "feed_key": "ranksnaxx-short-drops",
  "default_platform": "tiktok",
  "default_content_kind": "shortform_clip",
  "default_metadata": {
    "content_lane": "viral_rank_clip"
  },
  "items": [
    {
      "source_url": "https://www.tiktok.com/@creator/video/123",
      "external_id": "tt-123",
      "title": "Unexpected comeback",
      "creator": "@creator",
      "platform": "tiktok",
      "tags": ["comeback", "sports"],
      "metrics": {
        "views": 1200000,
        "likes": 88000,
        "comments": 9400
      },
      "clip": {
        "start_seconds": 2,
        "end_seconds": 17
      },
      "metadata": {
        "angle": "ranking payoff clip"
      }
    }
  ],
  "urls": [
    "https://clips.example.test/drop/secondary"
  ]
}
```

The adapter does not scrape or download media. It turns trusted URLs and operator
or server-supplied context into discovery candidates so the rest of Katcha can
score, review, acquire, edit, render, and publish through the same pipeline.

## Importing source drops

Use `POST /v1/discovery/sources/{source_id}/imports` when an operator, private
server, queue, or future connector has a batch of URLs ready for a configured
`operator_feed@v1` source.

```json
{
  "batch_key": "ranksnaxx-drops-2026-09-26-01",
  "urls": [
    "https://www.tiktok.com/@creator/video/123"
  ],
  "items": [
    {
      "source_url": "https://www.instagram.com/reel/example/",
      "platform": "instagram",
      "metrics": {
        "views": 120000
      }
    }
  ],
  "default_metadata": {
    "operator": "sundance"
  }
}
```

When `batch_key` is present, Katcha derives a stable run key:
`source-import:{source_key}:{batch_key}`. Reposting the same batch returns the
same discovery run instead of duplicating work. The import batch key and item
count are also added to candidate metadata through the operator feed adapter.
