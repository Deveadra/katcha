# Katcha Trend Intelligence

## Purpose

Trend intelligence is the decision layer between discovery/acquisition and editorial production. It answers four questions for each channel:

1. What topic is beginning to move?
2. How quickly is its rate of attention changing?
3. How much independent evidence supports the breakout call?
4. Is there enough channel fit and remaining headroom to justify production now?

The engine is designed to identify early acceleration, not simply rank already-popular items.

## System boundaries

### Discovery and acquisition

Discovery answers what source material exists. Acquisition and rights qualification answer what Katcha may ingest or reuse. Trend intelligence never treats public visibility, popularity, or a downloadable URL as permission to republish media.

Trend signals are metadata/evidence observations. Evidence packets may carry acquisition and rights references, but the trend score does not override those decisions.

### Trend intelligence

Trend intelligence owns:

- channel-scoped watch profiles;
- immutable, reusable signal snapshots;
- canonical topic association;
- deterministic time-series features;
- channel-specific opportunity scores and lifecycle state;
- evidence-packet generation;
- trend lifecycle events and durable refresh orchestration.

### Editorial and production

Downstream AI/editorial systems consume qualified evidence packets. They should not have to rediscover the topic from scratch. The packet preserves source URLs, timestamps, metrics, claims, media references, and acquisition/rights references.

## Data model

### ChannelTrendWatchVersion

A versioned channel strategy record. Raw trend observations are shared globally, but watch strategy is isolated per channel.

Important controls include interests, exclusions, entities, platforms, languages, regions, source weights, freshness horizon, minimum confidence, and opportunity threshold.

Platform, language, region, and entity filters are enforced before a topic can become a channel opportunity.

### TrendSignal

An immutable observation of one external entity at one point in time. Repeated observations use the same provider/external ID with distinct observation keys. Engagement counters remain cumulative in storage.

The scorer converts those cumulative observations into deltas before calculating velocity. This prevents repeated polling from double-counting total views, likes, comments, or other counters.

### TrendTopic and TrendTopicSignal

TrendTopic is the canonical topic identity. TrendTopicSignal links source observations to a topic while preserving match confidence and reasons.

P6.2a supports deterministic normalization and explicit topic association. Live discovery adapters and the P6.3 bridge may add stronger provider-specific identity and cluster evidence while retaining this evidence-bearing link.

### TrendOpportunity

A channel-specific, run-keyed snapshot containing lifecycle, opportunity score, confidence, rank, component values, reasons, prediction horizon, expiry, and evidence summary.

The same TrendTopic can therefore receive different scores for different channels without duplicating raw source observations.

### TrendEvidencePacket

A content-hashed, versioned downstream packet. It contains the topic thesis, why-now reasons, sources, claims, media references, and acquisition/rights references. A packet is only generated when both the channel opportunity threshold and minimum confidence are met.

## Deterministic scoring

Baseline ranking is local and token-free. The scoring model uses cumulative-counter deltas and multiple time windows to derive signals including:

- velocity;
- acceleration;
- breakout relative to baseline;
- novelty;
- source breadth;
- independent-source corroboration;
- community intensity;
- persistence;
- channel fit;
- freshness;
- data quality;
- saturation and remaining headroom.

The lifecycle states are:

- `emerging`
- `accelerating`
- `breaking_out`
- `peaking`
- `saturated`
- `cooling`

Single-independent-source spikes receive a confidence cap. This is intentional: a massive spike from one community or provider is useful evidence, but it must not be treated as equivalent to corroborated movement across independent sources.

## Cost policy

Routine polling, normalization, dedupe, feature calculation, ranking, and thresholding must remain deterministic and local wherever practical.

Paid-model semantic enrichment belongs after cheap gates. An LLM may enrich a qualified packet or resolve an ambiguous semantic association, but it must never overwrite raw observations, provenance, rights state, or deterministic safety constraints.

## Orchestration

Trend refreshes run on the dedicated Temporal trend task queue. A refresh:

1. loads the latest channel watch version;
2. selects fresh canonical topics;
3. applies channel platform/language/region filters;
4. applies entity constraints;
5. converts eligible signal snapshots into time-series samples;
6. computes deterministic scores;
7. ranks channel opportunities;
8. emits lifecycle events only for threshold-qualified opportunities;
9. builds evidence packets for qualified opportunities.

Run keys make opportunity creation idempotent for a channel/topic/run tuple.

## P6.3 discovery-to-trend bridge

External RSS/Atom, YouTube, and Reddit polling remains owned by the discovery subsystem. P6.3 does not repoll providers. It maps already-persisted `DiscoveryObservation` records into the shared trend core.

Bridge rules:

- only completed discovery runs are eligible;
- each observation receives the stable trend observation key `discovery-observation:<id>`;
- queue-backed observations use the stored cross-source cluster label as the canonical topic when available;
- unclustered observations fall back deterministically through candidate title, explicit provider query, watch/include terms, then canonical source identity;
- cluster-backed topic association receives stronger match confidence than deterministic fallback association;
- YouTube independence is keyed conservatively to channel identity, Reddit to subreddit/community before individual author identity, and RSS/Atom to feed identity;
- source URLs and candidate IDs are carried as media references, but the bridge explicitly records that reuse permission and rights are not inferred;
- bridge execution is idempotent and does not create a second provider-acquisition stack;
- optional channel refreshes are handed to the existing Temporal trend workflow with deterministic workflow and run keys;
- bridged signals can be inspected by discovery run, candidate, or queue lineage.

This separation lets Katcha collect external evidence once, reuse it across channels, and still preserve channel-specific trend strategy and scoring.

## API surface

The mounted control surface includes:

- `POST /v1/channels/{channel_profile_id}/trends/watch-profile`
- `GET /v1/channels/{channel_profile_id}/trends/watch-profile`
- `POST /v1/trends/signals`
- `POST /v1/channels/{channel_profile_id}/trends/refresh`
- `GET /v1/channels/{channel_profile_id}/trends/opportunities`
- `GET /v1/trends/opportunities/{opportunity_id}/evidence`
- `POST /v1/trends/bridge/runs/{discovery_run_id}`
- `POST /v1/trends/bridge/watches/{topic_watch_id}/queues/{queue_key}`
- `GET /v1/trends/bridge/signals`

Provider credentials and provider-specific polling behavior remain outside these channel-scoped trend contracts.

## Events

Core events include watch-profile versioning, signal observation, qualified opportunity lifecycle events, evidence-packet readiness, and discovery-bridge completion. Event payloads use IDs, counts, scores, timestamps, and non-secret metadata. Provider credentials must never be emitted.

## Live discovery integration contract

Live adapters for Reddit/community sources, YouTube metadata/trailers, and RSS/news/developer feeds produce metadata-first discovery observations with durable cursors and provider-specific backoff. P6.3 normalizes those persisted observations into TrendSignal-compatible evidence instead of creating another polling path.

Adapters and the bridge preserve official/developer source identity where available. Media references are candidates for the acquisition/rights subsystem, not automatic republication authorization.

## P6.2c calibration contract

Calibration must connect selected TrendOpportunity records to resulting productions/publications and compare predictions with actual Phase 5 channel outcomes.

Required channel-specific measures include breakout precision, false-positive rate, lead time, lift versus channel baseline, and calibration by confidence band. Learned influence must be gated by sample size and chronological/holdout quality; it may adjust ranking but may not erase deterministic rights or safety constraints.

## Success criterion

The trend subsystem is successful when acting on its qualified opportunities produces measurable positive channel growth with useful lead time. The number of topics discovered is not a success metric by itself.
