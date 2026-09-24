# Trend activation performance optimization

P7.3 measures whether Katcha's qualified trend opportunities actually become timely, profitable publications and produces bounded advisory changes for the P7.2 activation policy.

## Measurement boundary

The funnel is keyed by unique TrendOpportunity identity, not activation-scan rows. Repeated scans and Temporal retries therefore do not inflate conversion counts.

For each opportunity:

1. the earliest root ShortEpisode defines the plan event;
2. the first real Publication.published_at defines successful publication;
3. the latest analytics snapshot for that publication supplies views and revenue;
4. the existing 24-hour TrendOpportunityOutcome supplies lift and realized-breakout labels;
5. root ShortEpisode estimated cost supplies the activation-attributed production cost.

Repeated analytics samples are never summed for revenue.

## Snapshot metrics

Each immutable TrendActivationPerformanceSnapshot records:

- unique opportunities inspected
- opportunities planned
- opportunities published
- attributed 24-hour outcomes
- opportunity-to-plan conversion
- plan-to-publish conversion
- median opportunity-to-plan latency
- median plan-to-publish latency
- median opportunity-to-publish latency
- average expiry headroom at planning and publication
- latest publication views
- latest covered publication revenue
- root-plan estimated cost
- activation-attributed contribution margin
- revenue coverage / monetary-scope context
- mean 24-hour lift
- realized breakout rate
- current activation-policy version
- current calibration version
- current economics snapshot identity/headroom
- latest missed/deferred reason per unplanned opportunity

The snapshot is versioned per channel and idempotent by channel + run key.

## Recommendation safety

Recommendations are deterministic and advisory. P7.3 never calls an AI provider and never creates a new policy version itself.

A recommendation may adjust only throughput/quality fields:

- minimum opportunity score
- minimum confidence
- maximum activations per day
- maximum backlog
- cooldown minutes

It never proposes changes to:

- rights-readiness requirements
- source-health gates
- review gates
- publication gates
- production-eligibility rules
- budget-headroom minimums

## Sample sufficiency

Policy tuning is withheld until at least:

- 8 published activated opportunities
- 5 attributed 24-hour outcomes

Profit optimization also requires monetary coverage for at least half of published activated opportunities. Missing revenue is treated as missing data, not zero revenue.

## Recommendation states

### insufficient_data
Not enough published/outcome samples. Hold policy.

### insufficient_economics
Behavioral data exists, but revenue coverage is too weak to judge profitability. Hold policy.

### production_bottleneck
Fewer than 60% of planned opportunities reach publication. Katcha recommends reducing intake pressure rather than activating more trends.

### tighten_quality
Published throughput is adequate, but activation-attributed margin is non-positive or mean lift is below baseline. Score/confidence thresholds rise by small bounded increments and daily capacity may contract.

### expand_capacity
Activation-attributed margin is positive, mean lift is at least 1.10x, plan-to-publish conversion is at least 75%, and opportunities are being missed because of capacity/cooldown limits. Capacity expands by one bounded step.

### hold
Current observed quality, profitability, and capacity do not justify a change.

## Durable execution

Performance refresh runs automatically inside the existing channel-intelligence refresh/schedule. A dedicated one-shot ChannelTrendActivationPerformanceWorkflow is also available for operator/API refreshes.

No new worker or scheduler is introduced.

## API

- POST /v1/channels/{channel_profile_id}/trends/activation-performance/refresh
- GET /v1/channels/{channel_profile_id}/trends/activation-performance
- GET /v1/channels/{channel_profile_id}/trends/activation-performance/history

The API exposes evidence and recommendation snapshots; applying a recommendation remains a separate explicit activation-policy update.
