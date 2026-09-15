# Katcha Architecture

## Product boundary

Katcha is an independently deployable media intelligence, production, publishing, analytics, and channel-learning system. It owns canonical media, editorial outputs, durable workflows, model/TTS spend, YouTube publication lineage, performance observations, and channel operating policy.

Aerith is intentionally **not** a runtime dependency. External controllers use Katcha's versioned HTTP API and cursor-based domain-event contract. Katcha never imports Aerith code or exposes secret-bearing event payloads.

## Core principles

1. **Durable orchestration, not chained background jobs.** Temporal owns long-running workflows, retries, recovery, and scheduled intelligence refreshes.
2. **Deterministic work stays local.** Hashing, probing, dedupe, numeric ranking, scheduling statistics, and policy evaluation do not consume LLM tokens.
3. **Content-addressed canonical media.** Raw clips are shared evidence; channel-specific editorial outcomes are isolated.
4. **AI is routed by task and channel policy.** Provider choice respects availability, quality floors, expected value, and spend headroom.
5. **Cost is domain data.** Every paid model/TTS response is written to `usage_events`; channel-scoped calls reserve spend before provider execution.
6. **Learning is reproducible.** Training observations freeze pre-publication features separately from post-publication labels, and ranking snapshots store their model inputs, coefficients, validation metrics, confidence, and blend ratio.
7. **Human review is the default.** Automation promotion is explicit and evidence-gated. Phase 5 records authorization/readiness state; it does not silently enable public publishing.
8. **Retry safety is designed, not assumed.** Paid-call ambiguity fails visibly; deterministic stages converge idempotently; refresh runs have stable run keys.
9. **The operator hard cap is absolute.** Earned reinvestment may expand the active allocation only up to the configured hard monthly ceiling.
10. **External cursors are scope-bound.** A consumer key cannot be reused across channel streams and accidentally skip another channel's events.

## System topology

```text
                              +--------------------+
source URL -----------------> | ClipIngestWorkflow |
                              +----------+---------+
                                         |
                                         v
                              canonical clips / S3
                                         |
                                         v
                              +---------------------+
                              | ClipAnalysisWorkflow|
                              +----------+----------+
                                         |
                              local + AI features
                                         |
                    +--------------------+--------------------+
                    |                                         |
                    v                                         v
        +-------------------------+             +--------------------------+
        | ShortProductionWorkflow |             | LongformCompilationFlow  |
        | script -> TTS -> render  |             | select -> edit -> TTS    |
        +------------+------------+             | -> 16:9 render            |
                     |                          +-------------+-------------+
                     +--------------------------+-------------+
                                                |
                                           human review
                                                |
                                                v
                                    +-------------------------+
                                    | YouTube PublishWorkflow |
                                    +------------+------------+
                                                 |
                                       resumable upload/status
                                                 |
                                                 v
                                    +-------------------------+
                                    | Analytics workflows     |
                                    +------------+------------+
                                                 |
                                                 v
                              +------------------------------------+
                              | Channel Intelligence Refresh       |
                              | observations -> ranking ->         |
                              | economics -> schedule -> policy    |
                              +------------------------------------+
```

Dedicated workers isolate media ingest, analysis, production, long-form compilation, publishing, and channel intelligence failure domains.

## Data ownership

### Shared canonical intelligence

`source_items`, `clips`, and clip-analysis records describe the underlying media. They can be reused across channels. Sharing a canonical clip does **not** share a channel's private strategy, budget, ranking outcome, review history, or publication state.

### Channel-scoped editorial output

`productions` and `compilations` may carry `channel_profile_id`. New multi-channel flows create them with explicit channel scope before any paid editorial work occurs. Child regeneration inherits the same scope. Legacy/unscoped editorial records remain supported and are attributed conservatively when published.

A channel-scoped production or compilation may only be published through the YouTube connection owned by that `ChannelProfile`.

### Publication lineage

A `publication` references exactly one source:

```text
production_id XOR compilation_id
```

The database enforces this constraint. Analytics, retention, revenue, and publication failures remain traceable to the exact editorial generation and canonical source evidence.

## Self-sustaining intelligence model

### Channel profiles and immutable strategy versions

`channel_profiles` bind operational state to one YouTube connection. Mutable policy is represented by immutable version rows:

- `channel_strategy_versions`
- `automation_policy_versions`

Changing a strategy creates a new version and advances the active-version pointer. Previous policy remains inspectable.

### Ranking observations

`performance_observations` deliberately separate information by time:

- `features`: information that existed **before** publication;
- `labels`: views, retention, sharing, comments, subscriber conversion, and monetary outcomes measured **after** publication.

This prevents outcome leakage into the learning features.

For Shorts, features are reconstructed from the frozen `Production.analysis_snapshot`, not today's mutable clip analysis. Long-form observations use the frozen compilation-segment evidence that actually drove the episode.

### Learned ranking

Katcha trains numeric ranking locally. Paid LLMs are not used to fit coefficients.

Cold start remains the existing deterministic score. A learned snapshot stores:

- feature names;
- means/scales;
- coefficients and intercept;
- chronological training cutoff;
- sample count;
- holdout validation metrics;
- confidence;
- learned/baseline blend ratio.

A learned model receives zero influence when data is too sparse or it fails to outperform the deterministic baseline. Even a successful learned model remains blended rather than replacing the baseline outright.

Long-form candidate ordering applies an additional conservative adjustment to the channel score before diversity-aware sequence optimization.

## Budget and reinvestment model

Katcha has two budget layers.

### System-wide emergency ceiling

`KATCHA_AI_BUDGET_USD_MONTHLY` remains the deployment-wide safety ceiling. It can stop paid execution even when an individual channel still has channel headroom. It should be sized for the maximum aggregate spend the deployment owner is willing to permit.

### Per-channel strategy

Each channel strategy has:

- `monthly_base_budget_usd`: normal operating allocation;
- `monthly_hard_budget_usd`: absolute operator ceiling;
- `reinvestment_rate`;
- `reinvestment_cap_usd`.

Month-to-date economics calculate:

```text
margin = MTD revenue - MTD attributed AI/TTS cost
reinvestable = min(max(margin, 0) * reinvestment_rate, reinvestment_cap)
effective_budget = min(hard_budget, base_budget + reinvestable)
headroom = max(effective_budget - actual_spend - active_reservations, 0)
```

Reinvestment therefore **cannot** silently raise the hard monthly ceiling.

Costs include channel-scoped productions/compilations even when they are rejected or never published. Shared clip-analysis cost is allocated across channels using that clip. Legacy unscoped editorial cost is allocated across its publishing channels rather than counted in full for each channel.

YouTube analytics snapshots are cumulative from a video's publication date. For videos published before the current month, Katcha only treats the delta from a pre-month baseline snapshot as month-to-date revenue. If that baseline is unavailable, the old video's revenue is marked incomplete and is not used to justify reinvestment.

Economics snapshots expose revenue, source/shared cost, contribution margin, reinvestable amount, base/hard/effective budget, actual spend, active reservations, headroom, daily burn rate, projected month-end spend, and whether monetary scope is available.

## Paid-call reservations

A channel-scoped paid call obtains an `ai_budget_reservations` row before provider execution.

The reservation path:

1. locks the channel profile;
2. expires stale reservations;
3. computes live economics/headroom from authoritative history;
4. validates the task quality floor and provider availability;
5. selects the provider/model;
6. reserves the estimated amount under an idempotency key;
7. performs the paid call;
8. atomically writes `usage_events` and settles the reservation with actual cost.

This prevents two concurrent calls from spending the same remaining channel headroom. A provider failure before an accepted response releases the reservation. A crash after an accepted paid response remains an explicit ambiguous-call condition rather than being automatically retried and potentially charged twice.

### TTS consistency

TTS uses the same reservation/routing ledger. The first narration segment may choose the allowed provider from channel policy; subsequent segments pin the exact voice/model for that production or compilation. Budget pressure is not allowed to switch host voice halfway through an episode.

## Scheduling intelligence

Scheduling uses only performance evidence available at computation time. Recommendations are calculated in the channel's configured IANA timezone and include:

- weekday/hour;
- score;
- sample count;
- confidence;
- evidence source.

Sparse channels use configured fallback windows with low confidence. Operator blackout windows are removed from recommendations. Katcha does not claim causal timing effects from tiny samples.

## Automation policy

Every channel starts at `review_required`.

Policy levels are:

1. `review_required`
2. `auto_approve_low_risk`
3. `auto_publish_private`
4. `auto_publish_scheduled`

Promotion is an explicit operator action and can only advance one level at a time. Evidence includes:

- reviewed-item count;
- approval rate;
- explicit rejection rate;
- regeneration rate;
- publication failure rate;
- ranking confidence;
- recent negative-review rate;
- recent-review drift versus historical quality.

A promoted channel can be automatically demoted to `review_required` when gates deteriorate. Promotion state is an auditable authorization/readiness contract. Phase 5 does not, merely by existing or by starting an intelligence worker, enable automatic public publication; an execution path must explicitly consume the authorized level.

## External control / Aerith contract

The control plane exposes channel strategy, learning, economics, scheduling, automation, channel-scoped production/compilation creation, and domain events under `/v1`.

External controllers consume events with a stable `consumer_key`. The first read binds that key to either:

- one `channel_profile_id`, or
- the unscoped/global stream.

The same key cannot later move to another scope. ACKs advance monotonically by `(created_at, event_id)`. Secret-bearing keys such as tokens, credentials, encrypted values, upload URLs, and code verifiers are recursively removed from event payloads.

Important control endpoints include:

```text
POST /v1/channels
GET  /v1/channels/{channel_profile_id}
POST /v1/channels/{channel_profile_id}/strategy
POST /v1/channels/{channel_profile_id}/intelligence/refresh
GET  /v1/channels/{channel_profile_id}/ranking
GET  /v1/channels/{channel_profile_id}/economics
GET  /v1/channels/{channel_profile_id}/schedule
GET  /v1/channels/{channel_profile_id}/automation
POST /v1/channels/{channel_profile_id}/automation/promote
POST /v1/channels/{channel_profile_id}/clips/{clip_id}/productions
POST /v1/channels/{channel_profile_id}/compilations
GET  /v1/control/events
POST /v1/control/events/{event_id}/ack
```

## Reliability requirements

Before a workflow is considered production-ready:

- retries are bounded;
- deterministic side effects are idempotent;
- paid calls use conservative retry boundaries;
- paid model/TTS responses are cost-accounted;
- channel paid calls reserve headroom before execution;
- workflow failure remains visible through domain state/API;
- durable media is committed before database pointers claim success;
- strategy and learned state are versioned and reversible;
- channel budgets/private outcome state do not leak across channels;
- external event streams contain no secret-bearing payload fields.

## Phase status

### Phase 0 — Foundation ✅

FastAPI, Temporal, PostgreSQL/Alembic, S3-compatible storage, content-addressed ingestion, source lineage, outbox, usage ledger, task routing.

### Phase 1 — Media intelligence ✅

Keyframes/contact sheets, perceptual similarity, local transcription, structured features, bulk/deep vision funnel, deterministic scoring, durable analysis workflow.

### Phase 2 — Short production ✅

Versioned host persona, script candidates, TTS abstraction, caption timing, Remotion rendering, review/regeneration, per-production cost lineage.

### Phase 3 — Publishing and feedback ✅

YouTube OAuth, resumable upload/scheduling, publication workflow, analytics/retention snapshots, experiment lineage, safe retries.

### Phase 4 — Long-form compiler ✅

Performance-aware candidate selection, diversity sequencing, Sol editor/Gemini critic, canonical 16:9 reconstruction, durable TTS/rendering, review/regeneration, shared publishing/analytics.

### Phase 5 — Self-sustaining intelligence

Channel strategy isolation, leakage-safe observations, learned ranking, budget-aware model/TTS routing, spend reservations, reinvestment/economics, scheduling recommendations, promotion/demotion policy, dedicated intelligence refresh worker, and Aerith-safe event cursors.
