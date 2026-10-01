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
- `web_scout@v1` for grounded public-web discovery across unknown websites,
  creators, communities, TikTok, Instagram, X, Bluesky, YouTube, Reddit, and
  Discord pages without pre-registering every profile.

Future adapters should implement `DiscoveryAdapter` and register themselves in
`katcha.acquisition.adapters`. The rest of the pipeline does not need to know
whether the source is TikTok, Instagram, Twitch, X, a private server, or a new
site that appears later.

## Adapter catalog

Use `GET /v1/discovery/adapters` to see every installed discovery adapter and
the source types, platforms, query fields, credentials, and sample query shape it
supports. This is the contract the Editing Control Center or any ingestion UI can
use to guide source setup without hardcoding adapter knowledge.

Example response item:

```json
{
  "key": "operator_feed",
  "version": "v1",
  "label": "Operator Feed",
  "source_types": ["operator_drop", "server_drop", "url_batch", "private_feed"],
  "supported_platforms": ["tiktok", "instagram", "youtube", "twitch", "x"],
  "query_fields": ["feed_key", "default_platform", "items", "urls"],
  "required_credentials": [],
  "supports_imports": true,
  "sample_query": {
    "feed_key": "ranksnaxx-short-drops",
    "default_platform": "tiktok",
    "items": [
      {
        "source_url": "https://www.tiktok.com/@creator/video/123",
        "platform": "tiktok"
      }
    ]
  }
}
```

Manual source registration is no longer the only discovery path. For public content,
`web_scout@v1` can search outward from a channel's topics and discover URLs that
were never registered in Katcha. Returned candidate URLs are accepted only when
they are present in the web-search provider's grounded source list.

In the Content sources page, choose **Discover new sources**, select an active
channel, enter a topic, and optionally narrow the search to social platforms.
Saving the source does not start or schedule searches. **Search now** starts one
live AI and web-search request when the OpenAI key, live execution mode, and AI
budget are configured; provider charges may apply. Completed activity offers a
source-scoped count and links to up to five observed candidates. For recurring
hourly scouting, use the Katcha AI Command Center's confirmed source-scout action.

For a private server or non-public source, use `operator_feed@v1` when it can
provide URLs or JSON drops. Native platform adapters remain preferable where an
official API offers stronger freshness, metrics, pagination, or reliability; the
web scout acts as broad coverage and a fallback for sources Katcha did not know to
watch yet.


## Autonomous source scouting

The Katcha AI Command Center can turn a request such as
`Find new sources across TikTok, Instagram, X, and Bluesky` into a confirmed
`start_source_scout` action. The action creates a channel-scoped topic watch and
starts its durable schedule. By default it:

- searches once per hour with `web_scout@v1`;
- inherits topical terms from the request, channel interests, or the channel's
  latest topic watch;
- uses domain filtering when the operator explicitly names social platforms;
- carries a bounded rolling set of recently discovered URLs into the next cycle,
  encouraging exploration of adjacent creators, communities, sites, and newer posts;
- stores grounded results as ordinary discovery candidates so existing trend
  scoring, clustering, review, acquisition policy, and audit trails still apply;
- enforces both a daily source-poll cap and a provider web-search call quota.

This is metadata-first discovery. It does not bypass downstream acquisition policy,
and it does not make a discovered candidate publishable merely because Katcha found it.

## Source purpose / usage modes

The Sources UI presents the two common choices in plain language:

- **Find content for review** → `candidate_review`. Finds may be offered to Clips for operator review. Nothing is published automatically.
- **Research only** → `discovery_only`. Finds can inform trends, packaging, audience, and editorial context, but they are not offered as clip candidates.

Advanced modes remain available behind disclosure:

- `operator_authorized`: operator controls whether a candidate can proceed.
- `render_allowed`: the source is production-intended after normal rights, originality, and approval checks.
- `blocked`: keep the source configured but prevent normal use.

`usage_mode` is operational metadata, not a hardcoded rights decision.

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

## Sources workspace

Open `/ingestion` to manage Katcha's source network.

### Source Library

The library is server-paginated and designed for hundreds or thousands of sources.
It does not render every source into a select menu.

Use:

- search by source name or key;
- channel / shared-scope filter;
- source-type filter;
- active / paused filter;
- purpose filter;
- recent / name / created sorting;
- bounded next / previous pages.

Selecting a source opens its inspector. The inspector shows:

- channel scope;
- source purpose;
- what the source watches;
- active / paused state;
- total checks and failed checks;
- success rate;
- last check;
- total and unique finds;
- recent discoveries;
- recent check history;
- the exact recorded provider/configuration error when a check fails.

`GET /v1/discovery/source-library` provides the paginated/filterable library.
`GET /v1/discovery/sources/{source_id}/overview` provides the source-level
operational summary, recent runs, and recent finds.

### Add source

1. Choose the job: paste links, discover new sources, search YouTube, watch a
   YouTube channel, search Reddit, follow a feed, or use an advanced installed
   connector.
2. Name the source and choose an active channel or explicitly keep it shared /
   unassigned.
3. Configure what the source watches.
4. Choose its purpose: **Find content for review** or **Research only**. Advanced
   usage modes stay behind disclosure.
5. Choose exactly what happens after save:
   - **Save & check now**: persist the source and immediately run one check.
   - **Save only**: persist the source without starting a check.
6. Confirm and save.

Saving a source **never implies a recurring schedule**. Generic source
`poll_interval_minutes` remains configuration metadata, not a scheduler guarantee.
Durable recurring scouting is configured through the separate topic-watch /
source-scout automation path.

Paste-link collections save first; links are added from the source inspector and
each import creates an explicit discovery check.

Run and import requests retain idempotency keys in the current tab. If execution
dispatch cannot be confirmed, queued activity can be started again without adding
duplicate source records.

A shared source is unassigned, not broadcast to every channel.

`GET /v1/discovery/sources/{source_id}/runs?limit=50` remains the bounded
newest-first run-history endpoint. Provider failures preserve the most specific
recorded provider/configuration message rather than replacing it with a generic
Temporal wrapper where possible.

For future interface changes, follow [the product UX guidelines](UX_GUIDELINES.md).
