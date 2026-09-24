# Controlled autonomous trend activation

P7.2 adds a zero-token planning loop between qualified TrendOpportunity records and the existing P7.1 deterministic ShortEpisode activation path.

## Boundary

Autonomous activation creates or reuses **planned** ShortEpisode records only. It does not start paid script generation, TTS, rendering, review approval, or publication. Those stages continue to use the existing automation and review controls.

The scanner calls the same P7.1 `preview_trend_activation` and `activate_trend_opportunity` services used by the manual API. There is no second planner.

## Versioned channel policy

Each channel can version an activation policy containing:

- enabled / disabled state
- minimum raw opportunity score
- minimum confidence
- optional minimum calibrated score
- minimum `rights_readiness` diagnostic
- minimum remaining opportunity lead time
- maximum root trend activations per channel-local day
- activation cooldown
- maximum unfinished ShortEpisode backlog
- minimum latest economics budget headroom
- maximum opportunities inspected per run

A new policy creates a new immutable version. Historical runs preserve the policy version they used.

## Hard gates

The activation run is deterministic and provider-free. Before planning, Katcha requires:

1. a current non-expired opportunity snapshot;
2. the policy score/confidence/calibration thresholds;
3. sufficient rights-readiness diagnostic;
4. enough remaining lead time;
5. healthy configured discovery-source coverage;
6. a current economics snapshot with enough budget headroom;
7. daily capacity remaining;
8. cooldown elapsed;
9. backlog capacity remaining;
10. a successful P7.1 preview with enough currently production-eligible clips.

P7.1 then rechecks the active trend watch, evidence packet freshness, analysis state and current production eligibility. A trend score never grants media reuse permission.

## Capacity accounting and retry safety

Daily caps and cooldowns are based on persisted root ShortEpisode plans, not decision-log rows. Re-running a workflow therefore cannot consume daily capacity twice.

Each run has a channel + run-key uniqueness constraint, and each run records at most one immutable decision per opportunity. P7.1's deterministic activation identity continues to prevent duplicate episode plans for the same channel/opportunity/evidence/format/item-count identity.

If a worker fails after P7.1 persisted a plan but before P7.2 recorded its decision, the retry detects the persisted activation identity and records the recovered activation rather than creating a second episode.

## Decision ledger

Every inspected opportunity records an explicit reason, including:

- `activated`
- `already_activated`
- `threshold`
- `confidence`
- `calibrated_threshold`
- `calibrated_unavailable`
- `rights_readiness`
- `lead_time`
- `source_health`
- `budget`
- `backlog`
- `daily_cap`
- `cooldown`
- `not_ready`

The snapshot stores the policy, source-health summary, economics snapshot identity/headroom, channel-local daily usage, backlog and P7.1 preview summary. No credentials or provider payloads are written to events.

## Temporal orchestration

P7.2 reuses the existing intelligence worker and task queue.

- one-shot: `ChannelTrendActivationWorkflow`
- recurring: `ChannelTrendActivationScheduleWorkflow`

The recurring workflow continues-as-new after a bounded number of cycles.

## API

- `POST /v1/channels/{channel_profile_id}/trends/activation-policy`
- `GET /v1/channels/{channel_profile_id}/trends/activation-policy`
- `POST /v1/channels/{channel_profile_id}/trends/activation-scan`
- `POST /v1/channels/{channel_profile_id}/trends/activation-schedule`
- `GET /v1/channels/{channel_profile_id}/trends/activation-runs`
- `GET /v1/trends/activation-runs/{run_id}/decisions`

A disabled or absent policy cannot autonomously plan content. A schedule may remain durable while a policy is disabled; its runs terminate without activation until a later version enables the policy.
