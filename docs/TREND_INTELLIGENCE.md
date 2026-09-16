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

P6.2a supports deterministic normalization and explicit topic association. P6.2b adapters may add stronger provider-specific alias/entity extraction while retaining this evidence-bearing link.

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

## API surface

The mounted control surface includes:

- `POST /v1/channels/{channel_profile_id}/trends/watch-profile`
- `GET /v1/channels/{channel_profile_id}/trends/watch-profile`
- `POST /v1/trends/signals`
- `POST /v1/channels/{channel_profile_id}/trends/refresh`
- `GET /v1/channels/{channel_profile_id}/trends/opportunities`
- `GET /v1/trends/opportunities/{opportunity_id}/evidence`

P6.2b will add adapter/source-health control surfaces rather than embedding provider credentials or provider-specific behavior in these core contracts.

## Events

Core events include watch-profile versioning, signal observation, qualified opportunity lifecycle events, and evidence-packet readiness. Event payloads use IDs, scores, timestamps, and non-secret metadata. Provider credentials must never be emitted.

## P6.2b integration contract

Live adapters for Reddit/community sources, YouTube metadata/trailers, and RSS/news/developer feeds must produce TrendSignal-compatible observations. They should also own cursors, quotas, backoff, source-health state, and provider-specific rate-limit behavior.

Adapters should prefer metadata-first collection and preserve official/developer source identity where available. Media references are candidates for the acquisition/rights subsystem, not automatic republication authorization.

## P6.2c calibration contract

Calibration must connect selected TrendOpportunity records to resulting productions/publications and compare predictions with actual Phase 5 channel outcomes.

Required channel-specific measures include breakout precision, false-positive rate, lead time, lift versus channel baseline, and calibration by confidence band. Learned influence must be gated by sample size and chronological/holdout quality; it may adjust ranking but may not erase deterministic rights or safety constraints.

## Success criterion

The trend subsystem is successful when acting on its qualified opportunities produces measurable positive channel growth with useful lead time. The number of topics discovered is not a success metric by itself.
