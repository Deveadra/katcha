# Publication packaging

Katcha treats YouTube packaging as versioned publication lineage. A packaging variant is
immutable; activating it is a separate durable operation.

## P9.1 boundary

This slice supports:

- a title and description;
- an optional PNG or JPEG thumbnail stored under a publication/version namespace;
- immutable thumbnail size, MIME type and SHA-256;
- manual, idempotent activation on a publication that already has a YouTube video ID;
- durable title/thumbnail activation history.

It does **not** automatically choose a variant, rotate packages, or infer a winner. Reach
and click-through reporting belongs to P9.2, and learned recommendations belong to P9.3.

## Object layout

A thumbnail must already exist in Katcha's object store under:

```
packaging/<publication-id>/<variant-key>/v<version>/<filename>.png
```

JPEG files may use `.jpg` or `.jpeg`. Katcha reads the object when the variant is
created, verifies the actual image signature and YouTube's current size ceiling, then
freezes its SHA-256. Activation fails if the object bytes later differ. Do not overwrite
a variant's object; create a new variant version.

## API flow

Create an immutable variant:

```http
POST /v1/publications/{publication_id}/packaging/variants
```

Example body:

```json
{
  "variant_key": "hook-a",
  "version": 1,
  "title": "The ending nobody saw coming",
  "description": "The original publication description.",
  "thumbnail_storage_key": "packaging/<publication-id>/hook-a/v1/thumbnail.png",
  "created_by": "operator"
}
```

Activate it:

```http
POST /v1/publications/{publication_id}/packaging/activations
```

```json
{
  "variant_id": "<variant-uuid>",
  "idempotency_key": "manual-hook-a-v1"
}
```

List the immutable variant and activation history with the corresponding GET routes.

## Mutation semantics

Activation uses the existing publishing Temporal task queue and the publication's existing
YouTube OAuth connection.

1. Validate publication, channel connection, frozen variant and video ID.
2. Update the YouTube snippet while preserving the publication's tags and category.
3. If present, verify the stored thumbnail still matches the frozen size/hash and upload it.
4. Mark the activation applied and freeze the active packaging lineage into
   `Publication.treatment_metadata.active_packaging`.

Provider mutations have one automatic attempt. If the network dies after YouTube may have
accepted a mutation, Katcha records the failure instead of blindly spending provider quota
again. A new explicit activation key is required for operator recovery. If the title and
description already match on recovery, the text mutation is skipped locally.

Legacy publications with no packaging variants continue through the existing publishing
workflow unchanged.

## Future performance loop

P9.2 will ingest YouTube reach reporting (thumbnail impressions and CTR) and attribute
maturity-matched reach to the active packaging interval. P9.3 may recommend variants only
after channel-scoped chronological validation. CTR alone will never be treated as a winner:
watch quality, retention and contribution margin remain part of the decision.


## P9.2 reach ingestion

Katcha uses YouTube's channel bulk Reporting API reach report
`channel_reach_basic_a1`. The report has daily `date`, `channel_id` and
`video_id` dimensions with `video_thumbnail_impressions` and
`video_thumbnail_impressions_ctr` metrics.

Manual synchronization starts with:

```http
POST /v1/integrations/youtube/{connection_id}/reach/sync
```

The existing per-channel intelligence cadence also schedules reach sync automatically
for active channels whose YouTube connection has Analytics reporting scope. Each
connection has one deterministic automatic workflow ID per Pacific report day, so
repeated six-hour intelligence refreshes reuse the same scheduled workflow. The
publishing worker performs all Reporting API work. The intelligence loop waits only
for scheduling, never for provider downloads; missing scope is reported as a skip,
and scheduling or reach-provider failure cannot stop ranking, economics, editing
performance, packaging intelligence, or publishing. A later cadence may consume
newly imported reach; manual sync remains available for recovery.

The publishing worker first lists provider reporting jobs and reuses an existing
`channel_reach_basic_a1` job when present. Only when no matching provider job exists
does it create one. Provider job creation has one automatic attempt because a timeout
after acceptance is ambiguous; the next explicit sync reconciles provider jobs before
considering another creation.

Each downloaded report is frozen by provider report ID and SHA-256. CSV parsing is
bounded by byte and row limits, requires the official reach columns, and maps rows only
to a publication on the same YouTube connection with the exact YouTube video ID.

### Packaging attribution

YouTube bulk report days are interpreted in `America/Los_Angeles`, including DST.

- If one package was already active before the report day and no package changed during
  that day, the row is attributed to that immutable variant.
- If a Katcha packaging activation occurred at any time during the Pacific report day,
  the row is marked `mixed` and excluded from package-specific training.
- If no Katcha packaging activation existed before the day, the row is marked
  `legacy` rather than being assigned to a variant.

The canonical observation key is publication + report date. Reimporting the same report
or an overlapping report cannot increase impressions twice. If two provider reports
disagree for the same publication/day, Katcha fails closed instead of silently replacing
one measurement.

Inspect imported daily reach with:

```http
GET /v1/publications/{publication_id}/reach
```

P9.2 does not pick a winner or mutate packaging. P9.3 will combine this reach evidence
with retention and contribution margin before making bounded recommendations.


## P9.3 guarded packaging intelligence

Katcha evaluates packaging as a **qualified-reach** problem, not a CTR-only problem.

For each immutable title/thumbnail variant, it builds a cached performance window only
from consecutive Pacific report days that were fully attributable to that one variant.
Mixed switch days and legacy/unmanaged days never enter package-specific training.

The default maturity window is seven full days. A window is not queried from YouTube
Analytics until an additional processing lag has elapsed, because the targeted Analytics
API can omit the newest dates until all requested metrics are fully processed.

For a mature window Katcha requests the exact same Pacific `startDate/endDate` used by
the reach evidence and stores:

- thumbnail impressions and impression-weighted CTR
- views and average view percentage
- midpoint audience retention near 50% elapsed video time
- interval revenue when the monetary scope is available
- publication lineage cost and the latest known cumulative publication revenue/margin

Exact evidence windows are immutable and keyed by their reach observations, so the
six-hour intelligence loop reuses them instead of repeatedly consuming Analytics quota.

### Recommendation gates

Recommendations are advisory only. P9.3 never invokes the P9.1 YouTube mutation path.

A measured candidate can be marked `prefer` only when all required evidence clears the
frozen policy snapshot:

- both baseline and candidate have at least 1,000 known impressions
- both have CTR evidence and the candidate improves CTR by at least 5% relatively
- average view percentage is known and does not fall by more than 3 percentage points
- 50% retention is known and does not fall by more than 0.05
- when monetary scope exists, cumulative publication contribution margin is known and
  non-negative

If monetary scope is unavailable, that fact is retained explicitly instead of treating
revenue as zero. A created but unmeasured candidate can be suggested as `test` once the
baseline has sufficient reach and watch-quality evidence. Failed gates produce
`observe` plus concrete blockers.

### Chronological validation

Recommendation history is ordered by candidate evidence-window end date. Katcha exposes
a chronological development/holdout audit once enough comparisons exist, but explicitly
reports `optimizer_trained=false` and `optimizer_reliable=false` in P9.3. This is a
guarded rules engine, not a statistically validated learned optimizer.

Inspect the current state with:

```http
GET /v1/channels/{channel_profile_id}/packaging-intelligence
GET /v1/channels/{channel_profile_id}/packaging-intelligence/history
```

The channel summary also includes the latest packaging-intelligence snapshot.


## P10.1 automated candidate generation

Katcha can now generate immutable packaging candidates from publication lineage instead of
requiring an operator to invent every title/description variant manually.

```http
POST /v1/publications/{publication_id}/packaging/generations
```

```json
{
  "generation_key": "initial-auto-packaging-v1",
  "candidate_count": 3
}
```

The creative context is compiled deterministically from the publication's channel-scoped
source lineage. Supported sources in this slice are single-clip Productions and ranked
ShortEpisodes. The context includes the frozen brand/edit blueprint, source analysis or
episode premise, selected editorial treatment, existing packaging titles, and the latest
channel packaging evidence when available.

Generation uses the existing budget-aware `AITask.METADATA` route. Before the provider
call, Katcha persists a durable `provider_call_started` boundary. If that call becomes
ambiguous, the same generation key is terminal and cannot silently spend again.

A successful structured provider response is frozen before variant persistence. This means
a crash while writing variants can resume locally without another model call. Generated
variant keys are deterministic from the generation record, so partial persistence is
idempotent.

Each candidate contains a title/description, variation family, editorial angle, exact
supporting facts copied from compiled grounding evidence, and a renderer/provider-neutral
thumbnail creative brief. The deterministic validator rejects duplicate families/titles
and supporting evidence that is not present in the frozen grounding facts.

Accepted candidates are persisted through the existing immutable
`PublicationPackagingVariant` service with prompt/model/context/brand/edit lineage in
`variant_metadata`.

P10.1 **does not** generate thumbnail bytes and **does not** mutate YouTube. Thumbnail
production is a separate paid/QC stage, and any later title/thumbnail activation must still
flow through the existing P9.1 activation ledger.

## P10.3 guarded packaging experiments

The channel intelligence cadence now evaluates `test` recommendations from the latest P9
packaging snapshot after refreshing that snapshot. Only an active channel with the active
`auto_publish_scheduled` policy can start an automatic experiment. Earlier automation
levels, unpublished videos, stale/provider-degraded evidence, candidates from another
publication, and an occupied experiment slot fail closed. A seven-day Pacific-day
cooldown follows the latest activation.

Starting requires an applied prior package to serve as a rollback target and a mature
baseline window (at least seven attributed days, 1,000 impressions, CTR, watch percentage
and midpoint retention). Thumbnail-changing candidates require their frozen renderer
manifest, parent/source/verifier lineage and SHA-256; the prior variant must have a frozen
thumbnail for restoration. The snapshot ID, prior/candidate variant IDs, baseline window,
policy version and hashes are frozen on the experiment ledger. A partial unique index
allows only one active experiment per publication across concurrent workers.

All forward and rollback activations use the existing P9.1 registration and publishing
workflow, with deterministic experiment keys. At the provider mutation boundary, automatic
activations recheck the active channel policy and experiment lineage. Demotion blocks
further automatic edits and records `needs_review` when recovery is uncertain.

The intelligence cadence observes a *new* packaging snapshot only after an activation is
applied. It considers a complete candidate window whose dates follow the activation and
whose report end is at least two Pacific days old. Mature degradation in watch percentage,
midpoint retention or a negative margin requests a rollback to the frozen prior variant.
Once the rollback activation applies, the ledger records `rolled_back`. A mature `prefer`
without blockers completes the experiment. Missing evidence keeps it observing; it never
converts an absent metric into a successful experiment. The publishing worker remains the
only executor of YouTube mutations.

Operator inspection and recovery endpoints are:

```http
GET  /v1/publications/{publication_id}/packaging/experiments/eligibility?candidate_variant_id={variant_id}
POST /v1/publications/{publication_id}/packaging/experiments
GET  /v1/publications/{publication_id}/packaging/experiments
POST /v1/publications/{publication_id}/packaging/experiments/{experiment_id}/refresh
```

The existing manual packaging activation endpoint remains available for deliberate
operator recovery. `needs_review` means an automatic workflow stopped without evidence of
a safe final YouTube state; the operator should inspect the activation history and the
video before proceeding.


## P10.2 source-grounded thumbnail rendering

P10.1 candidates carry a thumbnail creative brief but no image bytes. P10.2 converts that
brief into a real 1280x720 thumbnail using analyzed source keyframes and the publication's
frozen channel brand. The default path uses no image-generation provider and therefore adds
no paid AI call or synthetic factual content.

For single-clip Productions, Katcha deterministically selects the middle analyzed keyframe.
For ranked ShortEpisodes, it selects the middle keyframe from the #1 payoff clip. The
renderer applies brand palette/type treatment and optional brief text, then verifies PNG
signature, dimensions and object-store persistence.

An existing immutable packaging variant is never modified. Thumbnail production creates the
next version of the same variant key and freezes the parent variant ID, selected source key,
render manifest and renderer verification into variant metadata. Replaying the same parent
variant reuses that derived thumbnail version.

```http
POST /v1/publications/{publication_id}/packaging/thumbnails
```

```json
{
  "parent_variant_id": "<P10.1 variant UUID>"
}
```

This stage still performs no YouTube mutation; P9.1 remains the only packaging activation
path.
