# Katcha Autonomy Specification

**Document:** `KATCHA_AUTONOMY_SPEC.md`  
**Version:** 1.0  
**Status:** Normative target specification; **not** a claim of current implementation or production readiness  
**Last updated:** 2026-11-12  
**Repository:** `Deveadra/katcha`  
**Scope:** All 19 mandatory capabilities, their cross-system contracts, three end-to-end (E2E) production paths, recovery, acceptance evidence, and optional growth features.

## 0. Purpose, success definition, and authority

Katcha is an independent, autonomous, multi-channel media business platform. It must **discover valuable opportunities, acquire and understand permitted source media, research and plan, script and edit, render and validate, publish when authorized, measure real outcomes, and improve subsequent decisions**. The owner sets strategy, authorization, and budgets rather than operating each handoff. Katcha must not depend on Aerith or another external agent to complete its own workflows.

**Completion means integrated behavior**, not 19 individually checked feature boxes, a green CI run, mock data, a successful API response without a side effect, or a one-time manually assembled video. A production's complete lineage and operational outcomes must be inspectable. Never substitute a simulated result for a real-world acceptance claim.

This specification describes **required end-state behavior**. Existing models, migrations, APIs, security boundaries, and immutable evidence remain authoritative for how to implement it. Extend and version those contracts; do **not** introduce a parallel production system or rewrite accepted architecture without necessity. Useful implementation references include [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md), [`docs/AUTONOMOUS_E2E_IMPLEMENTATION.md`](docs/AUTONOMOUS_E2E_IMPLEMENTATION.md), [`docs/APPROVED_SHOT_STORYBOARD_HANDOFF.md`](docs/APPROVED_SHOT_STORYBOARD_HANDOFF.md), [`docs/FORESCENE_TRAILER_FASTPATH.md`](docs/FORESCENE_TRAILER_FASTPATH.md), [`docs/CONTENT_MEDIA_LIVE_VERIFICATION.md`](docs/CONTENT_MEDIA_LIVE_VERIFICATION.md), [`docs/RANKSNAXX_LIVE_PRODUCTION.md`](docs/RANKSNAXX_LIVE_PRODUCTION.md), and [`AGENTS.md`](AGENTS.md). Older documents can describe partial milestones; current accepted code, CI, and live evidence determine what actually exists.

### Non-negotiable operating principles

1. **No broken handoffs:** Every completed stage produces a durable, validated input for its next consumer, or a machine-readable blocked/recovery receipt. No invisible manual copying of IDs, media, or files.
2. **One authoritative data plane:** PostgreSQL owns durable domain records; R2/S3-compatible storage owns canonical media and generated artifacts; Temporal owns long-running workflow execution; versioned APIs/events expose state. Do not equate an R2 upload with a committed, eligible Clip.
3. **Evidence before claims:** Retrieval suggestions are not scene verification; official provenance is not a reuse license; generated output is not editorial acceptance; a successful upload request is not published-video readiness.
4. **Policy-governed autonomy:** Operate without routine intervention **inside** configured rights, publication, provider-spend, security, and quality boundaries. Unknown or unauthorized decisions block safely and are explained; do not fabricate approval.
5. **Channel isolation:** Canonical media may be shared if allowed. Channel budgets, identities, strategies, editorial work, publication connections, and outcomes must not leak between channels.
6. **Retry safety and reproducibility:** Deterministic stages are idempotent; potentially billable or externally mutating ambiguous outcomes are reconciled, not blindly repeated. Preserve immutable versions and provenance.
7. **Profitability is the objective, not output volume:** Select content by audience value, sustainable quality, rights eligibility, and expected economics. Measured outcomes improve future decisions; do not optimize solely for raw views.
8. **No regressions:** Work from accepted `main`, preserve data compatibility and protection mechanisms, use focused PRs with required CI, and never merge failing/unreviewed changes.

## 1. Mandatory capabilities (R01–R19)

Every `Rxx` is a **must-have**. Each requirement includes the minimum behavior and an observable completion gate. Labels are stable identifiers for agents and acceptance reports.

### R01. Multi-channel management and intelligence

**Must:** Create/configure/pause channel profiles; maintain independent YouTube connections, audience and format rules, branded assets, tone/voice, scheduling, strategy, editorial state, budgets, and analytics; run concurrent channel workflows without cross-contamination. Share only permitted canonical source media and reusable compute. Support RankSnaxx rankings and FORESCENE trailer/editorial profiles without hardcoded subject constraints.

**Gate:** Two channels concurrently process distinct jobs and publish/record against their own connections; attempts to cross-use private channel policy, credentials, review state, or publications are rejected.

### R02. Autonomous discovery and trend intelligence

**Must:** Periodically and reactively monitor configurable permitted sources (official channels, publishers, YouTube, feeds, communities, public pages as authorized); identify trends, launches, topical footage, and evergreen opportunities; normalize provenance, deduplicate, assess freshness, competitive saturation, relevance and rights uncertainty, then rank by channel fit, likely impact, and estimated production cost. Preserve source links and event timestamps. No fixed Xbox/default-topic prompt for unrelated channels.

**Gate:** A scheduler discovers and retains a current, real, channel-relevant opportunity and creates a durable candidate without operator-supplied topic or manual stage transition.

### R03. Autonomous media acquisition

**Must:** Fetch permitted source media through bounded, SSRF-safe adapters and authorized browsing, and accept standalone project-free user uploads. Support resumable, integrity-checked 3 GB+ footage transfers; enforce size, type, permission, and source restrictions; verify actual bytes, SHA-256, codecs, duration, streams and metadata; deduplicate and persist canonical R2 objects and Clip records. Provide recoverable staging, progress, failure classification, retention, and cancellation. Never infer rights from download success.

**Gate:** A real eligible source becomes a complete verified Clip; interrupted large transfer resumes without duplicate canonical objects; failed/unverified staging does not appear as ready footage.

### R04. Central Clip Library and media intelligence

**Must:** Browse, upload, preview, play, search, inspect provenance, organize, archive, and delete retained assets independently of projects. Search titles, sources, topics, transcripts, visual tags, semantic moments, and time ranges; manage canonical originals, proxies, keyframes, embeddings and usage references; enforce safe deletion and channel permissions; reconcile metadata against storage. The Clip Library must be usable from the web UI and by automation.

**Gate:** A standalone asset is found and reused in a later production without reupload; search returns verified references/timestamps; delete/archive operations honor existing usage and retention policies.

### R05. AI video understanding and contextual intelligence

**Must:** Inspect real audio/video for measured shots, scene changes, dialogue, entities/actions/temporal relations and source-relative timestamps. Use existing SceneIndex/measured-shot evidence, transcription, local inspection and budget-gated Twelve Labs/Jockey or other suitable adapters. Link model suggestions to actual decoded evidence; distinguish hypotheses from verified observations; include confidence/coverage, contradictions, abstention and current-byte verification. Support long media via bounded, durable indexing/chunking without assuming today's single-pass size caps can handle 3 GB.

**Gate:** Ground-truth annotations for real permitted footage show correct relevant intervals and meaningful negative abstentions; the system never promotes unverified character/action guesses into factual shot matches.

### R06. Autonomous research and fact verification

**Must:** Gather timely, attributable information across approved sources; distinguish announcements, sourced facts, interpretations, rumors and speculation; attach claim-level citations and publication dates; reconcile conflicting evidence; refresh time-sensitive assertions before release. Produce channel-suitable, source-backed research dossiers and gaps/uncertainties without fabricated sources.

**Gate:** A research-to-script audit traces material factual claims to current sources, flags unsupported claims, and blocks or revises unverified assertions.

### R07. Autonomous content strategy and planning

**Must:** Choose channel, content type, narrative angle, length, hook, source requirements, schedule, cost envelope, quality threshold and priority from discovery and performance evidence. Create executable, immutable/versioned briefs and storyboards/shot requirements; account for novelty, trend urgency, rights, audience fit, budgets, repetition and competition. Issue acquisition/research follow-up tasks automatically when evidence is insufficient.

**Gate:** A valid opportunity yields an actionable channel-specific production plan with exact required assets/coverage and budget admission; unmet requirements produce explicit gaps and routed repair work, not a guessed edit.

### R08. Autonomous scripting, narration, and storytelling

**Must:** Produce accurate, original, channel-specific scripts and timing/beat structure; offer high-quality hooks, pacing, transitions and revision on evidence gaps. Generate ElevenLabs or policy-selected TTS with a stable channel voice where narration is required; support narration-free trailer presentation; store script revisions, captions, transcripts, costs and beat-to-claim/footage alignment.

**Gate:** RankSnaxx countdown and FORESCENE long-form scripts pass factual/format review; generated audio aligns with persisted beats; trailer passthrough does not force narration.

### R09. Intelligent storyboarding and autonomous editing

**Must:** Map script beats to verified, rights-eligible canonical media and measured intervals; choose meaningful shot boundaries and cuts; apply pacing, reframing, transitions, captions, music/SFX and contextually appropriate reaction assets; preserve per-channel branding. Compile the existing Editorial/Storyboard/VisualMediaUse contracts and ensure render-time revalidation of byte hashes and rights. Detect missing/irrelevant shots and selectively acquire, replan or regenerate rather than silently filling with unrelated footage. Support both vertical Shorts and horizontal long-form; preserve editable revision history.

**Gate:** Real footage produces a complete storyboard that a renderer can resolve without human asset binding; visual evidence for major beats matches the script, with no out-of-interval or revoked-media use.

### R10. Rendering and automated quality assurance

**Must:** Produce durable, reproducible Remotion/FFmpeg renders and verified masters, including configurable 1080p/4K and aspect ratio variants. Validate media decode, frame/audio continuity, duration, loudness, black/frozen frames, caption safe areas and synchronization, branded elements, shot relevance, and output identity. Retain previews, attempts, quality evidence, hashes and targeted repair jobs; never equate "render completed" with "publish approved".

**Gate:** All three production types yield playable, inspected output masters or explicit quality failures; injected audio/video/render faults are caught before publication.

### R11. Copyright, rights, and monetization eligibility

**Must:** Record source, creator, license/permission, permitted use, restrictions, evidence, assessed-at time, revocation and reviewer/decision identity. Enforce rights checks at acquisition eligibility, media selection, render dispatch/promotion and publication. Separate official provenance from authorization to redistribute; separate transformation/original commentary from any guarantee of fair use or YouTube monetization. Track YouTube claims, restrictions, strikes and eligibility; audit overrides and fail closed on unknown permissions.

**Gate:** Unauthorized or revoked footage cannot reach a newly published artifact; a verified studio trailer without reuse permission is **not** silently considered licensed; claims/monetization statuses remain separate from video quality and publishing success.

### R12. Autonomous publishing and distribution

**Must:** Use channel-owned, securely stored YouTube OAuth; build accurate title, description, tags, chapters, captions, thumbnail and required disclosures; support private/unlisted/scheduled/public states under policy; execute resumable, idempotent uploads; reconcile uncertain upload attempts; verify YouTube processing, playback readiness, correct channel and metadata, and persist the actual video ID, privacy state, errors and lineage. Never publish publicly merely because a file rendered.

**Gate:** An explicitly authorized real master becomes the correct YouTube publication with verified processing; interruption/retry does not create duplicate videos; public publishing remains blocked unless the applicable automation and rights policies authorize it.

### R13. Automation orchestration and agent coordination

**Must:** Drive the full lifecycle with Temporal and durable domain events/queues. Use stable work IDs, prerequisites, leases/fencing, checkpointing, idempotency, retries/backoff, provider failover when safe, dead-letter recovery, concurrent resource admission and explicit blocked reasons. Each stage owns its committed receipt and immediately schedules the next eligible stage. Reconciliation after crashes must restore progress without human copying of references. Paid/external ambiguous outcomes must be reconciled before re-execution.

**Gate:** A production advances automatically through every authorized stage; crash/restart and duplicate-event tests converge to one correct result with traceable evidence and no accidental duplicate billable calls or publications.

### R14. AI command center and operational web application

**Must:** Provide usable channel, discovery, Clip Library, asset upload, production, storyboard, review/scrubber, queue, scheduling, AI command, publication, analytics, and diagnostics interfaces. Natural-language commands must bind to confirmed scoped actions rather than generic refusals or unsupported optimistic claims. Show real state, previews, current progress, actionable errors and uncertainty; preserve input on errors; integrate authentication; prohibit fake dashboards and dead controls.

**Gate:** A user can inspect and intervene in a production entirely through working UI controls and sees the same authoritative records/receipts used by workers and APIs.

### R15. Human review and progressively autonomous approval

**Must:** Support manual, supervised, conditional-autonomy and policy-governed full-autonomy modes, with immutable per-channel policy versions. Define explicit quality, rights, confidence, spend, channel-scope and publication thresholds. Promote autonomy only from demonstrable readiness evidence, allow operator override/rollback, and automatically demote on material errors. Default conservative behavior must remain in effect until explicitly configured; never interpret a task prompt as authorization to weaken live policy.

**Gate:** Eligible low-risk work progresses unattended under an existing approved policy; an ineligible or ambiguous case blocks, explains why, and cannot bypass approval by changing workflow path.

### R16. Infrastructure, security, and disaster recovery

**Must:** Operate through FastAPI, PostgreSQL/Alembic, Temporal, Oracle Cloud runtime, Cloudflare Access, secure service principals and R2-compatible media storage. Enforce network protections, least privilege, secrets isolation/rotation, healthy resource admission, storage/Docker image retention, startup recovery, monitoring, safe CI/CD, schema compatibility, controlled release/rollback, durable backups and independent restore verification. Remote acquisition must have valid Access application/policy authorization without weakening operator access.

**Gate:** Actual configured environment has verified worker/API connectivity, data durability, storage headroom and recovery receipts; a restart or tested restore preserves committed job and media state. Code/CI alone does not prove deployed readiness.

### R17. Cost management and financial controls

**Must:** Reserve/account AI, TTS, indexing, retrieval, cloud processing, storage and other paid requests by channel, project and provider; enforce deployment-wide emergency ceilings plus channel base/hard budgets and production admission caps. Prefer deterministic work and cached analysis. Unknown provider billing outcomes may not be blindly retried; provider credits/operations are not fabricated USD costs. Compare costs to actual performance and projected value.

**Gate:** Parallel workers cannot overspend the same reserved headroom; unknown billing stops safely; representative costs and unknown values are visible; no unauthorized provider charges are required to prove an offline test.

### R18. Analytics, learning, and revenue optimization

**Must:** Periodically ingest genuine YouTube impressions, CTR, views, watch time, retention, subscriptions, engagement, publication restrictions and revenue/RPM where permitted; attach all observations to the exact channel, production and policy versions. Measure production cost and net return, compare hooks/formats/schedules/topics, enforce sample-size/confidence safeguards and feed supported insights into later discovery, ranking, packaging and schedule decisions. No invented revenue or unsupported causal claims.

**Gate:** A published production receives actual available analytics; a subsequent decision records which measured features/policy update informed its selection. Missing permissions or sparse samples remain explicit unknowns.

### R19. Notifications, diagnostics, and autonomous recovery

**Must:** Provide deduplicated, severity-based Telegram app/diagnostics/Oracle notifications and operational history, with safe links/summaries or appropriately compressed previews. Trace each stage's health, attempt IDs, redacted errors and recovery paths. Automatically fix or retry permitted routine failures, stop unsafe repetition, and notify operators only for policy approvals or genuine unresolved exceptions.

**Gate:** A realistic recoverable failure produces one incident with attempted recovery and a final outcome; an unrecoverable rights/budget/credential block produces a precise, actionable alert without leaking secrets.

## 2. Required E2E workflows and handoffs

### 2.1 Shared lifecycle

```text
Channel strategy / schedule / budget
   ↓
Discover opportunity → rank & dedupe → research / fact-check
   ↓
Rights/provenance intake → acquire (or reuse Clip) → byte/codec verification
   ↓
Index/understand actual footage → verified shots & gaps
   ↓
Production brief / media coverage → script & claims → storyboard
   ↓
Narration / reaction assets / editorial assembly (skip if approved passthrough)
   ↓
Render or eligible source-preserving passthrough → technical + editorial QC
   ↓
Rights + quality + spend + publication policy gate
   ↓
YouTube upload/schedule → verify processing and video identity
   ↓
Analytics + costs + restrictions → learn → next discovery/production decision
```

**Every arrow is a first-class, automatically executed handoff**, not an instruction for the owner to move a file, click an approval when policy already authorizes the action, or paste an ID. When evidence is missing: emit a gap, route a repair task, or block with a concrete reason. A denied external action never becomes success through a status override.

### 2.2 W-A: RankSnaxx ranking Short

1. Monitor channel-relevant sources across entertainment/game/culture topics; select a timely top-five concept, not a fixed Xbox topic.
2. Acquire five *distinct, permitted* canonical clips and supporting reaction assets. Verify bytes, rights, measured moments and relevant subject matter.
3. Generate and fact-check the #5-to-#1 script, channel-specific lightly funny narration, captions, timings and beat-to-shot alignment.
4. Produce a vertical, fast-paced montage with contextually appropriate reactions, ranking labels and branding; render, inspect and repair.
5. Apply rights, spend, quality and publication gates; publish to the connected RankSnaxx channel only if authorized; verify YouTube video identity and ingest results.

**Pass:** A real, non-synthetic, rights-qualified five-clip video is produced with correct ordering, narration, shots, reactions, output receipts and authorized YouTube publication evidence. If five eligible clips are unavailable, block or re-source; do not silently use unverified substitutes.

### 2.3 W-B: FORESCENE official-trailer presentation

1. Detect a timely *official* film/game trailer and retain its actual publisher/source, release details, provenance and channel fit.
2. Establish **separate redistribution permission**. Official origin alone is insufficient. Acquire/verify the original file or reuse an existing Clip.
3. Select the appropriate presentation mode. An authorized **source-preserving fast path** may reference the original object without modifying or re-encoding it; add branding/editing only when allowed by format, rights and operator policy.
4. Generate accurate release packaging; apply readiness/rights gate; publish/verify under FORESCENE's channel credentials; monitor claims and performance.

**Pass:** The authorized presentation uses unchanged original video/audio when passthrough is selected and retains end-to-end rights, byte, channel and YouTube lineage. A trailer without reuse rights correctly blocks publication.

### 2.4 W-C: FORESCENE researched long-form editorial

1. Discover a promising film/game topic, trailer or noteworthy story; gather multiple credible sources and research questions.
2. Acquire permitted trailers, supporting visuals, stills and source material; index temporal events and dialog; distinguish speculation from confirmed facts.
3. Write a substantial original analysis with claim citations and narration; map major beats to **verified actual footage intervals**; iteratively acquire/rewrite if supporting visuals are missing.
4. Edit a coherent 16:9 cinematic episode with original commentary, timing, graphics and branding; render, validate both editorial coverage and technical quality, and repair.
5. Apply rights/policy gates; publish as authorized; store packaging and analytics, feeding outcomes into later FORESCENE topics.

**Pass:** A real long-form master has inspectable claim-to-source and beat-to-shot evidence, no invented scenes, valid output/playback and a verified authorized publication.

### 2.5 Cross-cutting recovery paths

For every workflow: verify interruption/resumption of multipart acquisition, failed indexing, provider timeout/ambiguous bill, blocked rights, missing shot, stale script/asset digest, render failure, restart during upload, duplicate trigger, lost notification and unavailable analytics. A recoverable condition retries/replans within policy; unsafe or unavailable conditions become durable terminal/blocked receipts, never silent skips. Dead-letter queues must be discoverable and resume-safe.

## 3. Interconnected systems and ownership

| System | Authority | Required integration |
| --- | --- | --- |
| FastAPI control plane + web UI | Authenticated commands, scoped reads, versioned APIs | Every user-visible action reflects durable domain state; no parallel mock store |
| PostgreSQL + Alembic | Opportunities, Clipping/Clip Library lineage, editorial versions, budgets, rights, publications, analytics, incidents | FK/unique constraints, channel scope, migrations, transactional receipts |
| R2/S3-compatible media storage | Canonical bytes, proxies, indexes, scripts, audio, render masters, evidence | Immutable/checksummed references; lifecycle reconciliation; large transfer support |
| Temporal + dedicated workers | Execution, timers, retries, checkpoints, reconciliation, workflow identity | Durable handoffs, stable idempotency/fencing and recovery after restart |
| Acquisition/browser/media adapters | Permitted source intake and byte verification | SSRF-safe routes, verified Clip creation, rights not inferred |
| Vision/Twelve Labs/Jockey/transcription | Grounded source-relative observations | Source identity + timebase + measured evidence; provider suggestion ≠ verification |
| LLM/TTS/Remotion/FFmpeg | Research, scripts, narration, edit assembly and actual media rendering | Channel policy, deterministic controls, spend admission, artifact verification |
| YouTube APIs | Publishing and observed public metrics | Per-channel OAuth, resumable idempotency, processing state, analytics lineage |
| Cloudflare Access/Oracle/security | Access and availability | Scoped service principals, health checks, release/recovery evidence |
| Telegram | Notifications | Correlated, redacted and deduplicated actionable events |

The source repository's existing domain object names (for example Clip, EditorialRun, SceneIndexV1, Storyboard, Production, Publication, channel strategy, automation policies and usage events) should be reused. **The contract below specifies required data semantics, not an instruction to create duplicate tables or a competing public API.** Map to current models explicitly and version backward-incompatible changes.

## 4. Canonical machine-readable handoff contract

Every stage MUST persist an inspectable **receipt/envelope** before marking the stage complete. Fields may live in existing related records; no single JSON blob or new database table is mandated. Every consumer validates the latest evidence, not merely the upstream status string.

### 4.1 Common envelope (all stages)

| Required logical field | Meaning |
| --- | --- |
| `schema_version`, `event_type`, `event_id` | Versioned payload identity; supported consumer versions |
| `workflow_id`, `job_id`, `correlation_id`, `idempotency_key` | Entire-run lineage, per-attempt identity and retry deduplication |
| `channel_profile_id`, `production_id` (when applicable) | Immutable channel/editorial scope; never inferred from active UI selection |
| `source_record_ids`, `upstream_receipt_ids` | Complete predecessor lineage, including failure/reconciliation attempts |
| `created_at`, `updated_at`, `observed_at` | Distinguish source observations from machine actions and recency |
| `policy_version`, `rights_assessment_id`, `budget_reservation_id` (as applicable) | Frozen decisions with current-state revalidation at sensitive gates |
| `state`, `reason_code`, `retry_class`, `attempt`, `next_action` | Machine-readable outcome and recovery route; human explanation separately |
| `evidence_refs`, `content_hashes`, `provider_request_ids` (as applicable) | Proof, bytes and external-operation reconciliation without exposing secrets |

**Allowed state semantics:** `pending` → `running` → `succeeded` or `blocked`/`failed`/`unknown_external_outcome`/`cancelled`. `blocked` means current policy, permission, quality, dependency or budget disallows continuation; `failed` is an attempted operation with conclusive failure; `unknown_external_outcome` prohibits blind retrial of a billable/upload action. Retrying a known failure creates a linked attempt; states are not overwritten to hide prior evidence. Terminal stages emit a durable event/receipt. Map these semantics to existing Temporal/domain enums without needless rewrites.

### 4.2 Stage payloads and invariant checks

| Boundary | Required logical payload | Consumer must verify |
| --- | --- | --- |
| **Discovery → planning** | `opportunity_id`, channel scope, normalized topic/tags, source URLs/provider IDs, observed/release timestamps, novelty/trend evidence, score/uncertainty, rights unknowns | Actual relevant candidate, fresh sources, stable dedupe key, strategy/budget fit |
| **Planning → acquisition** | `media_requirement_ids`, source candidates, permitted method, acquisition authorization, quota, desired formats and deadlines | Explicit authorization, network/source eligibility, resource and spend admission |
| **Acquisition → Clip Library** | `clip_id`, `source_id`, canonical `storage_ref`, SHA-256, byte count, ffprobe video/audio metadata, transfer receipt, rights/provenance record, validation state | Object HEAD/GET verification as appropriate; bytes match SHA; committed Clip, not incomplete staging |
| **Clip → semantic analysis** | `clip_id`, immutable media hash, rational source timebase, index/version, measured frame/shot intervals, transcript offsets, tags and negative/unknown observations, provider/model receipts | Exact source bytes, bounded observations, timestamp origin, distinction between hypotheses and verified claims |
| **Research → plan/script** | Dossier/claim IDs, citations, timestamps, source confidence, fact/speculation labels, unsupported claims, expected audience/format, gaps | Claim evidence is attributable/current; unsupported claims edited or blocked |
| **Plan/script → storyboard** | `production_id`, frozen script revision and digest, beat/requirement IDs, ordered selected `clip_id` + measured intervals, permitted uses, gap/coverage, visual/audio intent, channel brand/voice version | No missing asset or stale digest; beat interval fits observed source-relative bounds; current rights/bytes |
| **Storyboard → render** | Versioned edit recipe/Storyboard, frozen input manifest, canonical hashes, narration/audio/caption refs, output profile, cost authorization | All media resolve securely; origin/timebase math correct; immutable inputs and pre-render rights recheck |
| **Render → QC** | `render_attempt_id`, master storage ref + SHA, codec/frame/duration/audio metadata, caption/thumbnail refs, render logs, attempt status | Actual output bytes decode/play; content and technical checks independently pass |
| **QC → publishing** | `publication_candidate_id`, exact master SHA, quality verdict + evidence, rights assessment, automation-policy authorization, channel connection, metadata/thumbnail/caption versions, publication visibility/schedule | Revalidate current rights, budget, correct channel, quality and current permission; no duplicate publication |
| **Publishing → analytics** | Idempotent publish operation, actual YouTube `video_id`, connection/channel IDs, privacy/schedule/processing states, published URL, upload receipts and uncertainty | Remote YouTube video and processing state reconciled; not just HTTP upload success |
| **Analytics → learning** | Observation period, actual platform metrics/revenue availability, production/strategy/policy IDs, realized cost, sample size and confidence, selected changes | Real observations only; no invented income; insufficient evidence cannot justify unconditional policy escalation |
| **Any stage → operations** | Incident ID, severity, redacted failure, retry/reconcile history, blocked reason and recommended next safe action | Tracing and notification dedupe; no token/private URL leakage |

**Minimum validation rules:**

- All R2 references are resolved server-side from authenticated canonical identities; client and LLM text cannot confer rights or select arbitrary storage keys.
- Every frame/time selection uses **source-relative rational timebase**, measured bounds and immutable input SHA; provider-search timestamps and editorial guesses are never independently authoritative.
- Edits, provider outcomes, rights changes, brand revisions and publication decisions are **versioned**. Changes invalidate only affected descendants and trigger selective repair; stale generations cannot silently publish.
- `channel_profile_id` and YouTube connection ownership are checked throughout paid operations and publishing. Cross-channel references to shareable canonical Clips must still re-check applicable usage permissions.
- R2 assets, database records, Temporal executions and remote provider states are reconciled after partial failures. Storage presence does not substitute for database eligibility, and vice versa.
- Use transactional spend reservations and stable external operation identities. For ambiguous chargeable calls or YouTube uploads, first reconcile via provider/remote IDs; do not blindly repeat.
- Every completed handoff must be consumable by a worker **without the owner copying/pasting an ID or manually picking an approved shot**, when the policy permits automatic selection.

### 4.3 Example contract shape (illustrative, not a new API)

```json
{
  "schema_version": "1.0",
  "event_type": "editorial.production_ready_for_render",
  "event_id": "<event-uuid>",
  "workflow_id": "<temporal-workflow-id>",
  "correlation_id": "<run-uuid>",
  "idempotency_key": "<stable-stage-operation-key>",
  "channel_profile_id": "<channel-uuid>",
  "production_id": "<production-uuid>",
  "policy_version": "<immutable-policy-revision>",
  "upstream_receipt_ids": ["<script-receipt>", "<shot-receipt>"],
  "script_revision": "<immutable-script-revision>",
  "storyboard_revision": "<immutable-storyboard-revision>",
  "input_media": [
    {
      "clip_id": "<canonical-clip-uuid>",
      "sha256": "<64-hex-byte-digest>",
      "source_relative_start_ms": 12500,
      "source_relative_end_ms": 16900,
      "measured_evidence_id": "<verified-interval-id>",
      "rights_assessment_id": "<current-permission-id>"
    }
  ],
  "state": "succeeded",
  "evidence_refs": ["<redacted-immutable-evidence-reference>"]
}
```

The literal placeholders above are **schema illustration only**, never fixture production evidence.

## 5. Workflow policy gates and autonomous behavior

| Mode | Production behavior | Publication authority |
| --- | --- | --- |
| `manual` | Operator-driven approvals at configured stages | Explicit approval per publish |
| `supervised` | Work is prepared automatically, owner reviews gated output | Explicit approval per publish |
| `conditional` | Automatically advances when measured, versioned thresholds pass | Only policy-authorized scopes/visibility; unresolved issues block |
| `policy_governed_autonomous` | Fully unattended eligible runs with monitors and safe auto-repair | Only previously authorized connection, rights class, quality band and visibility; automatic demotion on violations |

**Always recheck before publication:** source rights/revocations; asset hash/identity; channel/connection ownership; quality verdict; schedule and audience rules; external platform policy; budget; current automation-policy version. Approval to develop software is **not** permission to spend money, ingest prohibited media, alter live Access policy, deploy production, or publicly redistribute copyrighted trailers. Without pre-existing authorization, finish the code and non-publishing evidence, and mark the live action as a named external prerequisite.

**Financial safety:** Overall emergency budget and per-channel hard ceiling are enforced independently. Provider billing units and operation counts are not USD estimates unless a current supported conversion exists. Unknown cost stays `unknown`, and any possible double-charge is reconciled before retry.

**Monetization safety:** Copyright claims, fair-use evaluation and eligibility for YouTube Partner Program are distinct from a successfully uploaded video. Do not guarantee monetization for reused trailers; prioritize original editorial value and authorized footage.

## 6. E2E acceptance and evidence matrix

Acceptance is **three layers**: (1) automated unit/contract/security tests, (2) integrated real-media nonpublishing acceptance, and (3) live environment/provider/YouTube acceptance where separately authorized. A test pass at one layer must never be labeled as success at another. Collect a compact, reproducible evidence bundle with timestamps, accepted `main` SHA, deployed SHA when applicable, channel IDs, relevant run IDs, artifact hashes, redacted provider receipts, CI links and explicit unresolved blockers.

| ID | Covers | Observable acceptance evidence |
| --- | --- | --- |
| A01 | R01 | Two independent channel profiles/strategies/YouTube identities; concurrent workflows and negative cross-channel access tests |
| A02 | R02, R07 | Real source discovery → deduplicated opportunity → scored/channel-scoped executable brief without operator topic injection |
| A03 | R03 | Authorized media transfer with verified canonical bytes, ffprobe, SHA and storage receipt; 3GB+ resume test using permitted asset |
| A04 | R04 | Standalone Clip Library upload → semantic/text search → preview → reuse → retention-safe archive/delete |
| A05 | R05 | Permitted real-video interval ground truth, temporal localization accuracy, abstention on negatives, complete model/shot evidence |
| A06 | R06 | Research facts/citations carried into script; false/unsupported claim is flagged and repaired/blocked |
| A07 | R08 | Channel-specific scripts, correct narration/no-narration paths, caption timing and immutable revisions |
| A08 | R09 | Full script-to-real-shot-to-Storyboard-to-edit handoff without manual media selection; verified clip coverage |
| A09 | R10 | Actual encoded master inspected for frame/audio/captions/brand/continuity; fault-injection test rejects corrupt output |
| A10 | R11, R15 | Revoked/unknown media, stale rights, missing review and unauthorized public publish all fail closed |
| A11 | R12 | Authorized private/unlisted or permitted public YouTube publish with correct channel/video ID, processing state and no duplicates |
| A12 | R13 | Temporal restart, duplicate events, worker crash, interrupted acquisition and uncertain provider/upload result reconcile safely |
| A13 | R14 | Real UI operations for Clip Library, production, queue, review and diagnostics match backend state; no dead/mock controls |
| A14 | R16 | Cloudflare Access service identity, storage/DB/Temporal health, backups, restoration, capacity and deployed release evidence |
| A15 | R17 | Concurrent reservation/budget limits, unknown-billing stop, cost receipts and cap enforcement |
| A16 | R18 | Real available analytics linked to publication; subsequent strategy decision cites measurement and confidence |
| A17 | R19 | Deduped Telegram incident notification, safe remediation/retry receipts, and nonrecoverable block escalation |
| A18 | W-A | Full real, rights-qualified RankSnaxx five-clip Short through verified authorized YouTube publication |
| A19 | W-B | Full rights-qualified FORESCENE trailer path, including exact media/audio preservation if passthrough selected |
| A20 | W-C | Full real-footage FORESCENE long-form research → measured storyboarding → rendered master → authorized publication |
| A21 | Cross-cutting | Recurring unattended workflow over a declared observation window with no hidden handoffs, no duplications, and documented exceptions |
| A22 | Cross-cutting | Performance feedback demonstrably influences a subsequent channel-scoped opportunity or schedule decision |

### Required acceptance report format

For **every** R01–R19 and A01–A22, record `PASS`, `FAIL`, `BLOCKED_EXTERNAL`, or `NOT_TESTED`, plus evidence links/IDs, the exact environment/commit, verifier, remaining gap, and next safe action. **Do not collapse `BLOCKED_EXTERNAL` into `PASS`.** Every purported E2E pass must identify actual media inputs, rights basis, real output hashes, final YouTube video IDs when publishing was permitted, and observed downstream state. Where rights or credentials prevent real publication, complete nonpublishing acceptance and report the specific missing prerequisite.

**Definition of 100% technical integration:** R01–R19 show implemented, wired, regression-tested behavior, and all A01–A22 tests that can be legally/operationally executed are passed; any externally blocked criterion is transparently pending, not represented as completed. **Definition of verified fully autonomous production:** All required live-authorized A18–A22 evidence exists in the actual operating environment over a meaningful observation window, without manual handoffs. The latter cannot be declared from static code inspection, synthetic footage, documentation or a single green CI run.

### Release safety and verification cadence

- Reconcile work with accepted `main` and existing migrations first; make incremental, backward-compatible changes and evidence-bearing PRs.
- Run lint, type/compile, unit, security, integration, UI and contract suites appropriate to touched components, including negative gates and resource/failure injections. Keep required CI green and enable auto-merge only with passing checks and reviews.
- Agents perform their own engineering verification throughout. The owner's final product acceptance is an **additional** step, not a substitute for agent testing.
- Pre-deploy readiness, real production deployment, external provider tests, platform publication and rights authorization are separate gates; none is silently implied by repo merge. Do not relax Access, funds or public-publishing policies to make acceptance green.
- Preserve backups and rollback routes. Maintain a known-good build/deployment and stop rather than conceal serious regressions.

## 7. Recommended workstreams and integration ownership

These are implementation assignments, **not** permission for subteams to declare independent E2E success.

| Agent | Primary ownership | Must hand to next system |
| --- | --- | --- |
| Astra 1: intelligence/media | R02–R07 and acquisition-side R11 | Rights-qualified canonical Clip/media-index evidence, research dossier, executable plan, exact source-relative intervals and gap receipts |
| Astra 2: production | R08–R10 and production-side R11 | Versioned script/storyboard, real rendered and verified master, QC/rights evidence and publication candidate |
| Astra 3: E2E integration | R01, R12–R19, final R11 gating, **all handoffs and every A01–A22 result** | Autonomous orchestration, UI, safe YouTube publication, analytics feedback, recovery and truthful acceptance matrix |

Astra 1 and 2 can work in parallel on distinct branches/worktrees, agreeing on existing contract semantics and avoiding conflicting migrations. Merge both through CI, then run Astra 3 against accepted `main`. Astra 3 owns correction of anything incomplete from either workstream. Agent prompts must be self-contained and refer to **this exact file** for all named requirements; never rely on the agent having access to an earlier chat.

## 8. Additional wants (NOT blockers for core autonomy)

Once the 19 must-haves and three workflows are integrated: FORESCENE editorial website and SEO/articles; TikTok/Instagram cross-posting; long-form-to-Shorts repurposing; thumbnail/title experiments; localization/dubbing; sponsorship/affiliate management; community engagement; generative reaction memes and custom motion graphics; optional virtual presenters; advanced competitor forecasting; cross-channel series/campaigns; licensed-asset collaborations; richer revenue forecasting; newsletters and audience-owned channels; additional documented editing-provider integrations. These may materially improve revenue, but **none** excuses a missing Clip Library, unreliable media understanding, broken editing handoff, unauthorized content use, or a missing publishing/analytics feedback loop.

## 9. Final operating standard

A healthy Katcha can repeatedly and safely choose a channel-relevant opportunity, obtain **permitted, verified real footage**, understand it, produce an original or explicitly authorized video, pass objective quality/policy gates, publish through the correct channel, inspect actual platform outcomes, and adjust future choices. It must survive routine faults, obey budget and rights constraints, expose meaningful operational evidence, and require humans only for strategic decisions or truly unresolved external authorization. **No partial implementation, feature-demo success, simulated media, or unverified live claim counts as the finished product.**
