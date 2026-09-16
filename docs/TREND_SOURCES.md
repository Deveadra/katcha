# Katcha Live Trend Sources

## Purpose

P6.2b turns the provider-neutral trend intelligence core into a continuously fed system. Live source adapters collect metadata and engagement observations from supported providers, persist durable cursors and health state, and normalize successful observations into the shared `TrendSignal` store.

Collection is metadata-first. Discovering a public image, trailer, video, enclosure, or URL does not grant Katcha permission to republish it. Adapter media references are emitted with `rights_status: unassessed` and remain candidates for the acquisition/rights subsystem.

## Supported adapters

### Reddit

The Reddit adapter uses public JSON search endpoints with an explicit Katcha user agent. It records post score, upvotes, comment counts, community identity, canonical Reddit URLs, timestamps, limited text metadata, and available media references.

Subreddit/community identity contributes an independent-source key. Rate-limit headers are retained as poll metadata. HTTP 429 responses become a `rate_limited` health state with bounded backoff; they do not create zero-engagement observations.

### YouTube

The YouTube adapter uses the existing authenticated YouTube integration and Data API client. Search results are expanded through `videos.list` so trend observations retain cumulative view, like, and comment counters, channel identity, published timestamps, canonical watch URLs, tags, thumbnails, and video references.

The adapter does not download YouTube media for trend scoring. Video and thumbnail references remain unassessed acquisition candidates.

### RSS / Atom

The feed adapter supports HTTP(S) RSS/Atom feeds, including developer blogs and news sources. It preserves canonical entry links, publication dates, categories, authors, summaries, enclosure/media references, ETags, and Last-Modified cursors.

Each newly observed feed entry contributes a `mentions` activity signal. Conditional requests use ETag and Last-Modified values so unchanged feeds can return 304 without creating duplicate activity.

## Channel subscriptions and global observations

`TrendSourceSubscription` is channel scoped. It contains provider strategy such as the query, feed URL, polling interval, cursor, status, and source health. These records are not copied into globally reusable trend signals.

`TrendSignal` remains global. Equivalent provider snapshots are deduplicated by provider ID, external item ID, cumulative metrics, canonical URL, and a configurable observation-time bucket. The default bucket is five minutes. This lets two channels watching the same provider reuse the same raw observation while retaining separate channel subscription strategies.

Channel subscription IDs are intentionally not stored in `TrendSignal.signal_metadata`. Poll lineage remains in `TrendSourcePoll` and domain events so channel strategy does not leak into the shared raw-observation layer.

## Durable polling

Each source has a durable cursor and a run-keyed `TrendSourcePoll` history. The `(trend_source_subscription_id, run_key)` uniqueness contract makes duplicate workflow execution idempotent.

Successful polling:

1. fetches provider metadata;
2. normalizes observations;
3. registers globally reusable trend signals;
4. advances the provider cursor;
5. clears prior failure/backoff state;
6. schedules the next poll;
7. emits a success event.

Failed polling:

1. creates no synthetic engagement observation;
2. preserves the previous cursor;
3. increments consecutive failures;
4. records a bounded retry/backoff window;
5. changes source health to degraded, rate-limited, or down;
6. emits a failure event without provider credentials.

Temporal schedules respect `retry_after_seconds` and sleep for the longer of the configured polling interval or provider backoff.

## Source health guard

Trend refresh is withheld when configured live-source coverage falls below `KATCHA_TREND_MIN_SOURCE_COVERAGE`, which defaults to `0.75`.

This is deliberately conservative. Missing provider observations must not be interpreted as falling audience interest. When coverage is degraded, Katcha returns `withheld_source_coverage` rather than producing lifecycle updates that could falsely mark a topic as cooling or saturated.

Channels with no configured live sources are not blocked, preserving manual/provider-neutral signal ingestion from P6.2a.

## Configuration

Important settings are:

- `KATCHA_TREND_HTTP_TIMEOUT_SECONDS` — provider HTTP timeout.
- `KATCHA_TREND_REDDIT_USER_AGENT` — explicit Reddit client identity.
- `KATCHA_TREND_FEED_USER_AGENT` — RSS/Atom client identity.
- `KATCHA_TREND_SOURCE_MAX_BACKOFF_SECONDS` — upper bound on exponential/provider-directed backoff.
- `KATCHA_TREND_OBSERVATION_BUCKET_SECONDS` — global observation dedupe bucket; default 300 seconds.
- `KATCHA_TREND_MIN_SOURCE_COVERAGE` — minimum healthy/unknown weighted source coverage before trend refresh; default 0.75.

Provider secrets are not accepted inside source query or source metadata JSON. YouTube credentials continue to live in the existing encrypted YouTube connection system.

## Control API

The trend router mounts the following source controls:

- `GET /v1/trends/adapters`
- `POST /v1/channels/{channel_profile_id}/trends/sources`
- `GET /v1/channels/{channel_profile_id}/trends/sources`
- `GET /v1/channels/{channel_profile_id}/trends/sources/health`
- `GET /v1/trends/sources/{source_id}`
- `POST /v1/trends/sources/{source_id}/status`
- `POST /v1/trends/sources/{source_id}/poll`

Creating a source starts its durable polling schedule by default. Manual polls accept an idempotency key and map it to a stable Temporal workflow identity.

## Reliability rules

Live collection follows these invariants:

- provider errors never become zero-valued trend observations;
- rate limits are backoff signals, not negative audience signals;
- cursors advance only after successful adapter collection;
- cumulative engagement counters remain cumulative in storage so P6.2a can calculate deltas;
- raw observations are globally reusable, while source queries remain channel scoped;
- no credentials/tokens are emitted in domain events or evidence packets;
- media discovery does not override rights qualification;
- Alembic must retain exactly one migration head.

## Next calibration layer

P6.2c will connect qualified trend opportunities to resulting productions/publications and compare predictions against real channel outcomes. It will measure lead time, breakout precision, false-positive rate, lift versus channel baseline, and confidence calibration without weakening deterministic rights or source-health safeguards.
