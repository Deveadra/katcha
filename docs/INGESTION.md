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
  "adapter_key": "manifest",
  "adapter_version": "v1",
  "platform": "tiktok",
  "usage_mode": "operator_authorized",
  "query_template": {
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
    "items": [
      {
        "source_url": "https://www.tiktok.com/@creator/video/123",
        "external_id": "tiktok-123",
        "title": "Unexpected comeback"
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
