# Katcha Architecture

## Product boundary

Katcha is an independently deployable media factory. It owns media acquisition, canonicalization, analysis, editorial generation, rendering, publishing orchestration, analytics ingestion, and performance learning.

Aerith is intentionally **not** a runtime dependency. Future Aerith control will use Katcha's versioned HTTP API plus domain events. This preserves independent operation, makes testing/deployment simpler, and prevents either product from inheriting the other's failure domain.

## Core principles

1. **Durable orchestration, not chained background jobs.** Temporal owns long-running workflows, retries, and recovery.
2. **Deterministic work stays local.** Hashing, ffprobe, dedupe, state transitions, and file manipulation do not consume LLM tokens.
3. **Content-addressed media.** Raw assets are keyed by SHA-256. Multiple source URLs can resolve to one canonical clip.
4. **AI is routed by task.** Callers request `bulk_vision`, `deep_video`, `short_script`, etc.; they do not hard-code providers.
5. **Cost is domain data.** Provider/model usage is recorded in `usage_events` and guarded by a monthly AI budget.
6. **External integration uses an outbox.** Katcha commits domain state and events together, then adapters publish them later.
7. **Human review is a first-class stage.** Automation can later be promoted to auto-publish by policy; it is not assumed initially.
8. **Every activity is idempotent.** A retry must converge on the same business state.

## Current vertical slice

```text
Client
  |
  v
FastAPI POST /v1/clips/ingest
  |
  +--> source_items (registered)
  |
  v
Temporal ClipIngestWorkflow
  |
  v
ingest_source activity (thread pool)
  |
  +--> yt-dlp download
  +--> ffprobe
  +--> SHA-256
  +--> S3/MinIO raw/<hash>
  +--> PostgreSQL ON CONFLICT dedupe
  +--> source -> canonical clip linkage
  +--> domain_events outbox row
  |
  v
source_items.ready + clips.ingested
```

The workflow identity is stable per source URL. Repeated submissions attach to the same source/workflow. A failed source can be explicitly retried, which creates a new workflow attempt while retaining the source identity.

## Data model

### `source_items`

Represents where Katcha found a piece of media. Source metadata belongs here because the same bytes can have different creators/captions/engagement values across reposts.

### `clips`

Represents canonical media bytes. `sha256` is unique and the raw storage key is derived from it. Future perceptual hashes and embeddings will augment exact-byte dedupe rather than replace it.

### `domain_events`

Transactional outbox. Future publisher workers can deliver events to webhooks, Aerith, NATS/Kafka, or another bus without coupling those systems to write transactions.

### `usage_events`

Model/API cost ledger. Every paid model invocation will record task, provider, model, units, estimated/actual cost, and the source/clip/production it belongs to.

## Workflow topology

Katcha will use multiple workflows instead of one giant pipeline:

```text
ClipIngestWorkflow
       |
       v
ClipAnalysisWorkflow
       |
       v
ShortProductionWorkflow -----> ReviewWorkflow -----> PublishWorkflow
       |                                              |
       |                                              v
       +-------------------------------> AnalyticsWorkflow
                                                        |
                                                        v
                                                LearningWorkflow

CompilationWorkflow consumes the canonical clip library + historical performance.
```

Separate workflows provide clean retry boundaries and allow us to re-analyze or re-render without redownloading media.

## Planned analysis funnel

```text
Discovery candidates
  |
  | metadata/source heuristics (no AI)
  v
Downloaded unique media
  |
  | local transcript + contact sheet
  v
Bulk vision route (cheap model)
  |
  | confidence / value threshold
  v
Deep video route (Gemini native-video path)
  |
  v
Feature record + deterministic scoring
```

AI produces structured features; Katcha's scoring engine combines those features with source metrics and eventually real YouTube performance. We do not let an LLM's arbitrary 0–100 opinion become the sole ranking function.

## Default AI task routing

Logical task routing currently encodes the agreed initial strategy:

- `bulk_vision`: OpenAI Luna -> Gemini Flash-Lite fallback
- `deep_video`: Gemini Flash -> OpenAI Terra fallback
- `short_script`: OpenAI Terra -> Gemini Flash fallback
- `longform_editor`: OpenAI Sol -> Gemini Flash fallback
- `metadata`: OpenAI Luna -> Gemini Flash-Lite fallback
- `performance_analysis`: OpenAI Sol -> Gemini Flash fallback

Provider API calls are intentionally not wired into the ingestion slice. When they are added, all calls must go through the router/budget ledger rather than directly from workflow code.

## Aerith integration contract

Aerith should eventually be able to:

- inspect system/worker health;
- submit source URLs and discovery jobs;
- request or cancel productions;
- query clip/production state;
- approve/reject review items;
- trigger publication/scheduling;
- retrieve channel analytics and recommendations;
- subscribe to domain events.

Katcha should never import Aerith code. The integration package will be an adapter over public Katcha contracts.

Candidate events:

```text
source.ingested
source.ingest_failed
clip.analyzed
clip.scored
production.rendered
production.review_required
publication.scheduled
publication.published
analytics.snapshot_ingested
budget.threshold_reached
```

## Storage lifecycle

Initial policy:

- raw published/high-value clips: retained;
- rejected raw clips: retention policy configurable later;
- hashes/source metadata/analysis: retained after raw deletion;
- renders: retained while operationally useful, then lifecycle-managed.

Object storage is S3-compatible so local MinIO can be replaced by Cloudflare R2/S3 without changing domain code.

## Reliability requirements

Before any workflow is considered production-ready:

- retries must be bounded;
- side effects must be idempotent;
- workflow state must be queryable;
- failure must be visible in API/UI;
- no silent fallback to a paid model;
- cost must be recorded for paid calls;
- temporary media must be cleaned;
- durable media must be committed before database pointers claim success.

## Build sequence

### Phase 0 — Foundation (current)

- standalone service boundary
- Postgres + MinIO + Temporal
- source ingestion API
- durable ingestion workflow
- yt-dlp/ffprobe download path
- exact SHA dedupe
- outbox and usage ledger
- AI routing policy

### Phase 1 — Media intelligence

- scene/keyframe extraction
- perceptual hash + local visual embeddings
- faster-whisper transcription
- contact-sheet generator
- cheap vision classification
- Gemini deep-video escalation
- feature schema + deterministic score

### Phase 2 — Short production

- host persona/versioning
- script candidate generation and judging
- TTS provider bake-off abstraction
- caption timing
- Remotion renderer
- review queue/API
- production cost accounting

### Phase 3 — Publishing and feedback

- YouTube OAuth and upload/scheduling
- analytics snapshots
- retention curves
- source/clip/production lineage
- model/host-style experiment tracking

### Phase 4 — Long-form compiler

- compilation candidate selection
- sequence optimization
- Sol editor / Gemini critic loop
- 16:9 recomposition from canonical raw media
- long-form render/review/publish workflow

### Phase 5 — Self-sustaining intelligence

- learned scoring from Katcha's own channel results
- budget-aware model routing
- schedule optimization
- automated reinvestment/accounting signals
- multi-channel isolation and shared clip intelligence
- Aerith control adapter
