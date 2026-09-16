# Katcha Trend Discovery

Katcha's trend-discovery layer is the metadata-first front end of the media pipeline. It watches configured topics across multiple sources, records repeated point-in-time observations, detects emerging momentum, clusters related stories without collapsing distinct media assets, and produces a bounded review queue before download or expensive AI analysis.

## Design invariants

- Discovery candidates are metadata records, not downloaded media.
- One story can have many distinct candidate assets and source URLs.
- Repeated observations are preserved so velocity and acceleration are measured rather than guessed from one snapshot.
- Trend scoring is deterministic and consumes no LLM tokens.
- Provider credentials live only in environment settings. Topic-watch JSON, database rows, and domain events must never contain API keys, OAuth tokens, passwords, or Authorization headers.
- Provider failure is isolated. A failed Reddit run must not discard successful YouTube or RSS results from the same watch execution.
- Queue rows are pinned to the immutable trend-score snapshot that earned the rank.

## Environment

```bash
KATCHA_YOUTUBE_DATA_API_KEY=
KATCHA_REDDIT_CLIENT_ID=
KATCHA_REDDIT_CLIENT_SECRET=
KATCHA_REDDIT_USER_AGENT="Katcha/0.1 trend-discovery"
```

RSS/Atom discovery needs no provider credential.

## Topic watches

A topic watch is immutable and versioned. Updating a watch means creating a new version rather than rewriting the historical definition used by prior executions.

Example request:

```json
{
  "watch_key": "gaming-xbox",
  "name": "Gaming / Xbox emerging topics",
  "include_terms": ["xbox", "gaming"],
  "exclude_terms": ["giveaway"],
  "language": "en",
  "locale": "en-US",
  "freshness_horizon_hours": 72,
  "max_candidates": 100,
  "adapter_configs": [
    {
      "adapter_key": "youtube",
      "adapter_version": "v1",
      "query": {"order": "date", "limit": 50}
    },
    {
      "adapter_key": "reddit",
      "adapter_version": "v1",
      "query": {"subreddit": "gaming", "sort": "new", "time_filter": "week"}
    },
    {
      "adapter_key": "rss_atom",
      "adapter_version": "v1",
      "query": {"feed_url": "https://example.com/gaming.xml"}
    }
  ]
}
```

Secrets inside `adapter_configs` are rejected. Provider adapters obtain their credentials from runtime settings.

## Provider adapters

### RSS / Atom

The `rss_atom@v1` adapter parses standards-based RSS and Atom feeds, filters include/exclude terms, preserves publication metadata and feed rank, validates public network destinations, and follows only a bounded number of redirects.

### YouTube Data API

The `youtube@v1` adapter performs metadata search with a bounded freshness window and cursor pagination, then hydrates each result through the video details endpoint. Observations can include views, likes, comments, search rank, publication time, channel identity, and description metadata.

### Reddit OAuth API

The `reddit@v1` adapter uses application-only OAuth with the configured client credentials. Tokens are cached only in worker memory. Listing pagination uses Reddit's `after` cursor. Observations can include score/upvotes, comments, search rank, publication time, subreddit, outbound URL, domain, and media hints.

## Trend scoring

`emerging-trend-v1` combines:

- freshness
- engagement velocity from repeated observations
- acceleration when at least three observations exist
- cross-source corroboration
- novelty
- saturation penalty

The scoring service stores every result as an immutable `candidate_trend_scores` row with the full component breakdown, observation window, algorithm version, and idempotent score key.

A source's raw engagement values are converted to a weighted engagement index before velocity is calculated. The point is not to compare a Reddit upvote directly with a YouTube view; it is to detect whether a candidate's source signal is growing quickly relative to its own earlier observations.

## Story clustering

Final watch execution applies deterministic title-token clustering across the candidates produced by successful provider runs. Related results can therefore corroborate one story while remaining separate candidate assets.

Example:

- YouTube: `Xbox reveals Project Nova gameplay trailer`
- Reddit: `Project Nova Xbox gameplay trailer revealed`
- RSS: `Studio explains Project Nova gameplay after Xbox reveal`

These may form one story cluster with three source keys and three distinct candidate IDs. Cluster evidence is carried on the review queue: cluster key, representative label, member candidate IDs, source keys, shared tokens, related-item count, and corroborating-source count.

## Durable execution

`TopicWatchWorkflow` is the one-shot orchestration path used by the API and future external controllers. It:

1. creates/reuses one discovery run per configured adapter;
2. runs each adapter as an isolated child `DiscoveryRunWorkflow`;
3. collects successful and failed child results independently;
4. clusters candidates from successful runs;
5. rescales final scores with corroboration/saturation context;
6. materializes the top-N durable review queue.

`TopicWatchScheduleWorkflow` repeats that same workflow at a configured interval. It periodically continues-as-new to keep Temporal history bounded.

## HTTP control plane

Important routes:

- `POST /v1/trends/watches`
- `GET /v1/trends/watches`
- `POST /v1/trends/watches/{topic_watch_id}/execute`
- `POST /v1/trends/watches/{topic_watch_id}/schedule`
- `GET /v1/trends/watches/{topic_watch_id}/ranked`
- `GET /v1/trends/watches/{topic_watch_id}/queue`
- `POST /v1/trends/watches/{topic_watch_id}/candidates/{candidate_id}/score`

Execution is idempotent when an `idempotency_key` is supplied. Queue materialization is also idempotent by topic-watch version and execution/queue key.

## Downstream boundary

Trend discovery does not download every result and does not decide final editorial use. It identifies what appears to be emerging and why. Selected candidates can then move into Katcha's existing downstream acquisition, ingest, analysis, production, review, publishing, and learning pipeline under the channel's configured workflow.
