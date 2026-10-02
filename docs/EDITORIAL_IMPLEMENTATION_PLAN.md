# Katcha researched originals: implementation and acceptance plan

Prepared 2026-10-02 from `Pasted markdown(1).md`. Baseline inspected: main
`2d0ef5eb511cd7c9fa37cdc8e4a5489ac3dabefc`. This is a delivery plan, not a claim
that the complete pipeline already operates. Track implementation below.

## Outcome

A channel-scoped prompt and source URL become a researched original episode:
source acquisition → observed video evidence → bounded investigative research →
verified dossier → story selection → cited script → supplementary asset acquisition
→ visual direction → narration and timing → deterministic render → operator review
→ existing private-upload/publishing lifecycle. One trailer is a valid source.

FORESCENE's first acceptance case is a “things you missed” trailer breakdown.
The implementation must also support game reveals, explainers, comparisons,
interviews and news analysis without hardcoded Marvel keywords or canned speech.
Original reporting, strong evidence, useful visual variety and recognizable branding
are the monetization strategy. No promise of revenue, rights clearance or retention
improvement follows merely from generating a script.

## Verified starting points

| Area | Existing integration | Required change |
| --- | --- | --- |
| Agent execution | `services/goal_tools.py`, goal runner, authenticated native routes | Add discoverable typed editorial tools; preserve channel and capability checks |
| Persistence | SQLAlchemy models, Alembic, `db.load_model_metadata`, DomainEvent | Add editorial identity and immutable revisions; never overload Compilation |
| Long-form | `longform/editor.py`, schemas, activities/workflows/worker | Existing prompt is a compilation editor; introduce original-episode stages |
| Rendering | `rendering/longform_manifest.py`, `renderer/src/longform-video.jsx` | Existing item kinds are clip/narration; version a separate editorial manifest |
| Providers | `ai/router.py`, provider policy, capacity, subscription, usage reservations | Reuse authority, free-first policy and accounting; add task-specific contracts |
| Acquisition | Source/discovery adapters, managed ingestion, analysis workflows | Resolve eligible source assets and actual analysis; do not treat a URL as watched video |
| UI/auth | Shared Aerith guidelines, native tool authorization and launcher | Add editorial work inside existing workspaces and secure APIs |

The attachment's exact model availability, pricing and quotas are not validated
facts for this implementation. Do not encode those numbers or treat a subscription
as API credit. Verify supported account capabilities when enabling each live route.

## Delivery ledger and order

| Milestone | Concrete deliverable | Dependencies | Initial status |
| --- | --- | --- | --- |
| E1 | Durable projects, evidence/script contracts, revision concurrency, authenticated APIs, native AI tool registration | Baseline | Implemented; CI passed on 6c55ae4 |
| E2 | URL/managed asset intake and measured video analysis attached to project | E1 | Intake/local-analysis implemented; richer interpretation pending |
| E3 | Durable specialist research, evidence verification and cost-bounded recursion | E1–E2 | Implemented; synthetic integration verified |
| E4 | Story selection, writer/critic/revision loop and cited script review | E3 | Implemented; live editorial acceptance pending |
| E5 | Claim-directed supplementary asset scout and acquisition/rights gate | E3–E4 | Planned |
| E6 | Visual director, timing, compiler and editorial Remotion composition | E4–E5 | Planned |
| E7 | Integrated editorial workspace, recovery/status and publication handoff | E1–E6 | Planned |
| E8 | Fault injection, real authorized trailer acceptance, rollout and analytics | E7 | Planned |

Use substantial PRs with coherent executable boundaries. E1 must explicitly say
that it stores and validates drafts; saving cannot imply research or rendering ran.
Later milestones must replace capability limitations only when their wiring is
actually shipped and tested. Do not expose a fake start button backed by fixtures.

## Project and artifact contracts

**Project:** UUID, required channel profile, immutable creative brief and primary
source URLs, target duration, request identity, input digest, generation/revision,
current state, actual blocker, timestamps, parent project if branched, budget policy.
Creation replay with the same identity and payload returns the same project;
changed payload with the same identity conflicts. No global/default channel fallback.

**Revision:** append-only snapshot of source observations, research sources,
claims, script and visual requests. Persist schema version, canonical digest and
creation actor. Save uses expected revision, with a database compare-and-swap;
concurrent saves must not overwrite each other. Scripts point to exact evidence
IDs in the same frozen revision. Editing evidence invalidates downstream approvals,
assets/timing/manifest where relevant; published history remains immutable.

**Observed source:** managed asset ID, media hash, measured duration, analysis ID,
transcript/shot/frame evidence, model provenance and analysis coverage. Native
video analysis and sparse frame sampling are different capabilities. Never say
“every frame watched” when only contact sheets or sampled frames were inspected.
Timecodes are source-relative; rendered timeline time is separate. Preserve audio
cues, OCR, uncertain character identifications and bounding regions as observations.

**Research source:** stable ID, canonical/original URL, title, source category,
published and retrieved timestamps, bounded excerpt/locator, retrieval receipt,
content hash, originating query and source lineage. Search snippets are discovery
leads; fetched original evidence is needed before verification. Preserve failed,
paywalled, deleted and contradictory sources as explicit limitations.

**Claim:** ID, claim text, observation and time range, source references, bounded
supporting evidence, classification (`confirmed`, `inference`, `theory`), confidence,
verification status, contradictions, unresolved questions and audience relevance.
Confidence is not proof. An official source is authoritative only for what it
actually states. Repeated syndications are not independent corroboration. A theory
never becomes confirmed merely because another model repeats it.

**Script beat:** ID, editorial role, narration, referenced claim IDs, planned duration,
on-screen uncertainty disclosure and visual intent. Factual beats need evidence;
pure transitions can be nonfactual. A selected claim must be verified or explicitly
retained as an attributed unresolved theory. Unresolved contradictions cannot be
silently spoken as confirmed fact. Preserve cold open, escalation, callbacks,
transitions and closing payoff without forcing a fixed item count.

**Asset request:** beat/claim IDs, semantic purpose, desired medium, subject/time
range, source preference, aspect ratio, urgency and fallback. **Acquired asset:**
original URL, upstream acquisition receipt, storage key/hash, trim bounds, credit,
rights/use review decision, decision actor/time and permitted uses. Discovery and
permission are distinct. Never accept an LLM-written `approved=true` as authorization.

**Visual beat:** script beat, acquired asset IDs, integer timeline frames, source
trim/time mapping, layers, normalized regions, captions/labels, motion and brand
tokens. **Render artifact:** versioned manifest hash, all upstream hashes, exact
renderer version, output key and actual measured output properties.

## E1: first implementation boundary

1. Add dedicated project/revision models and additive migration with indexes,
   foreign keys, uniqueness constraints and positive revision checks.
2. Add strict bounded Pydantic evidence/script schemas: reject extra fields,
   non-finite values, duplicate IDs, broken references, impossible time ranges,
   unsupported claims in script, and missing theory/inference disclosures.
3. Keep revisions immutable and audit project creation/saves transactionally.
4. Expose create/list/detail/history/save through channel-scoped APIs under existing
   authentication. Register native tools so the goal planner discovers real schemas.
5. Require observed project IDs for subsequent goal operations. Derive mutation
   request identity from the existing goal step, not model-invented strings.
6. Return capability status: draft storage available; autonomous research/render
   unavailable until implemented. Do not queue nonexistent workers.
7. Test request replay/mismatch, cross-channel access, permission denial, stale
   writes, history, malformed evidence, and native tool execution.

## E2: source intake and analysis

Accept URL, previously ingested media, and manual upload through existing acquisition.
Record one primary and optional reference assets; no minimum-three-clips restriction.
Wait for actual acquisition/analysis completion using durable child workflow IDs.
Map failed downloads, unsupported URLs, authentication walls, missing audio and
provider limits to recoverable project blockers. Reuse analysis by media hash and
analysis-policy version. A video replacing bytes at the same URL invalidates reuse.

Generate timestamped observations and questions from actual frames/transcript/video.
Check each time range against measured media duration. Store coverage (native video,
sampled frames, transcript-only) and missing segments. A prompt cannot invent a
character identity or symbol location into the observed record.

Acceptance: one authorized trailer becomes a managed source with measured duration,
analysis receipts and source-linked observations; failure resumes without downloading
or billing again when a successful receipt already exists.

## E3: specialist research orchestration

Use stateless activities on existing durable infrastructure, not persistent bots:
analyst, official researcher, lore/background researcher, community researcher,
verifier. The planner decides relevant specialties from the brief/observations.
The source scout discovers material; a separate retrieval/extraction contract
produces evidence for investigative research. Do not simply rename web discovery.

The planner outputs bounded questions with parent IDs, purpose and source strategies.
Canonical question/source fingerprints deduplicate work across workers. Child
questions retain lineage. Stop on configurable depth, query count, document count,
token/cost reservation, elapsed deadline, low new-evidence yield or operator cancel.
Initial conservative defaults: depth 2, 24 queries, 40 retrieved documents, 2
concurrent model tasks; calibrate against real quotas rather than promise capacity.
All limits are server policy maxima, never instructions a retrieved page can change.

Persist each successful retrieval and specialist result before downstream work.
Use stable workflow/activity identities: project, generation, stage, input digest,
question ID, provider-attempt ID. Reuse complete matching results; do not recompute
completed expensive stages because an unrelated branch failed. Limit history growth
with checkpointed batches/continue-as-new and store large payloads in object storage.

Verifier reads original sources, searches contradictions, distinguishes primary
evidence from speculation, and outputs checks linked to exact claims/source versions.
A second model is a useful critic, not an independent factual source. Missing
evidence becomes a question/blocker, never an invented citation.

## Provider and budget enforcement

Use existing channel execution mode and provider policy. Fixture data stays labeled
and cannot satisfy live acceptance. Prefer verified free-capable routes, then
supported included subscription routes when available. Paid fallback requires an
explicit nonzero authorized cap and capability. A `free_first` label alone must not
authorize paid traffic. Unknown billing eligibility blocks the call under zero budget.

Reserve budget atomically before calls across all workers; settle actual usage and
retain uncertainty for requests accepted before disconnect. Quota exhaustion pauses
with retry-after/next eligible time; it must not silently switch to a paid API.
Persist provider/model, route basis, units, reservation and external request identity.
Cancellation stops new calls and releases only unused reservations. Reconcile
ambiguous accepted requests; do not retry them blindly. Separate grounding/tool
costs, inference, TTS, media egress and rendering in the project cost report.

## E4: story and script

Select findings by credibility, novelty, viewer payoff and relevance, with diversity
and contradiction penalties. Do not force 10 facts when 6 are supported. Preserve
selection reasons. Generate beat-level narration grounded in the frozen dossier.
Critic checks opening promise/payoff, repetition, unsupported assertions, uncertainty
wording, factual completeness and pacing. Bounded revision loop (default 2); failed
quality gates return an editable draft plus specific blockers, never false approval.

Store draft, critique and revised script separately with exact input hashes. Operator
feedback applies to the selected revision unless explicitly saved as channel policy.
Target duration is a planning estimate until actual TTS or uploaded narration exists.
Voice-disabled channels must have a deliberate captions/original-audio workflow or
an explicit missing-narration blocker, not forced TTS.

## E5: supporting material

Generate asset requests per script beat/claim. Search official clips/stills,
interviews, earlier material, reference images, permitted stock and owned material.
Prefer assets with clear provenance and relevant evidence. Fetch through managed
acquisition with the existing rights policy; unknown/restricted assets cannot enter
render. Keep discoveries useful even when acquisition is blocked. Support manual
replacement and reviewer decisions without losing research.

Fallbacks must retain meaning: sourced quote card, original diagram or a relevant
approved frame. Do not fill missing assets by repeating the trailer indefinitely.
Generated explanatory illustrations must be identified as illustrations and never
used as evidence of an actual frame, character appearance or historical event.

## E6: visual direction and deterministic rendering

First ship video/image, source trim, freeze, slow-motion, crop/push-in, split-screen
comparison, circle, arrow, highlight, caption, label, quote/source card and restrained
icon. Follow with counters, timelines, maps and diagrams. Emoji is opt-in per channel;
bundle licensed/versioned graphics for consistent rendering rather than relying on
host emoji fonts. Never render model-provided JavaScript, HTML or remote executable SVG.

The visual director supplies semantic purpose and source-linked regions. Compiler
converts these to validated frame timings, normalized bounds, safe margins, brand
tokens and layer order. Track transformations through crop/resize/letterboxing so
circles point to the actual object. Freeze frames and source playback have distinct
time mappings. Reject unknown primitives; do not silently omit them.

Generate/measure narration before final timing. Reconcile script beat durations to
audio samples and integer frame boundaries. Enforce positive durations, contiguous
base visual coverage, source bounds, overlay bounds, permitted assets, caption safe
areas and font/media availability. Recheck rights decisions and source hashes at
compile/render time. Keep raw URLs out of renderer fetches; use trusted managed keys.

FORESCENE defaults: thin evidence ring, restrained white pointer, distinguishable
confirmed/inference/theory labels, readable comparisons, cinematic freeze/push-in,
approved watermark. Do not invent a new logo or overuse emoji.

Retention checks examine the combined script/timeline: repeated source intervals,
long unchanged visuals, excessive overlays, narration/visual mismatch and delayed
payoffs. A zoom on repeated footage does not magically count as fresh evidence.
Thresholds are configurable editorial heuristics, not guaranteed YouTube outcomes.

## E7: user journey and operational wiring

In Channel Studio/Production, add an Editorial Projects view with brief/source entry,
current stage, completed evidence, blockers, estimated/actual cost and resume controls.
Detail tabs: Research, Script, Assets, Storyboard, Preview. Evidence links open the
exact source/time; theories are visibly distinct. Preserve unsaved text and request
identity across reconnects. Distinguish empty, pending, unavailable and failed.
Use the shared Aerith shell and check keyboard/mobile behavior at 390px.

Katcha AI can create/inspect/revise/start/resume/cancel through the same typed native
tools and authorization as the UI. Match natural meaning using the existing LLM
planner. No keyword-only special case for “VisionQuest.” Long-running requests return
a project link plus durable progress, not an empty synchronous wait.

Workflow states: draft → queued → acquiring → analyzing → researching → verifying →
writing → sourcing_assets → directing → voicing → compiling → rendering → review.
Any active stage can become blocked, failed or cancelled. Terminal completion only
follows stored artifacts. Stage attempts are records, not overloaded status strings.

Commit an outbox/start intent in the same transaction as project state. A reconciler
starts/observes stable Temporal IDs so API timeout and worker restarts cannot strand
queued work. Register workflows/activities in the chosen worker, add startup recovery,
readiness/capability checks, queue concurrency, compose/launcher wiring and operator
activity events. Reconciliation must not resurrect cancelled or superseded generations.
Use compare-and-swap/leases to fence stale completions from replaced attempts.

Publishing reuses existing packaging, approval, private upload, scheduling and
analytics. New editorial publication sources need explicit registry support; do not
pretend an editorial project is a Compilation to bypass source validation. Publishing
requires current approved render, current rights review and existing channel authority.
Never auto-publish simply because a creative prompt requested a script.

## Security and failure behavior

Treat source pages, captions, comments and model output as untrusted data. Extracted
instructions cannot call tools, change budget or approve assets. Validate URLs,
redirects and resolved IPs in fetch infrastructure; reject internal/metadata/private
targets, constrain bytes/time/MIME, and isolate media parsing. No secrets in prompts,
stored errors or progress events. Enforce channel ownership for every resource lookup,
including read/history/export, native tools and eventual asset URLs.

Bound schema sizes, source excerpts, list lengths and recursive follow-ups. Store
full licensed/source material according to existing retention policy; keep excerpts
minimal. Rights review is a workflow decision, not an automatic legal judgment.

| Failure | Required behavior |
| --- | --- |
| API loses connection after save/start | Replay identity returns existing project/start receipt |
| Worker dies after provider accepts call | Mark uncertain, reconcile receipt; no blind rebilling |
| Quota exhausted | Pause with actionable next step and preserved evidence |
| Bad model JSON/unknown citations | Bounded repair or blocked stage; no fabricated defaults |
| One research branch fails | Keep successful branches, report coverage gap |
| Script changed after voice/assets | Invalidate dependent outputs, preserve old revision |
| Asset removed or permission revoked | Block compile/publication and offer replacement |
| Render fails | Reuse approved manifest/assets; existing bounded recovery |
| Cancel races with activity completion | Fencing rejects stale promotion; retain audit artifact |
| Restore DB/object store | Rebuild pending starts from receipts; verify artifact hashes |

## Acceptance and rollout

E1: API/database tests with synthetic fixtures, strict validation, optimistic
concurrency, request replay, channel security and native catalog schema resolution.
Run existing goal/auth/longform contracts to guard current behavior. Exercise additive
migration and downgrade on disposable storage, plus Postgres concurrency in CI.

E2–E4: deterministic workflow tests for one-trailer input, timestamp coverage,
recursive dedup/limits, contradictory sources, uncertainty disclosure, quota waits,
call ambiguity and recovery. Contract tests must prove every referenced activity is
registered and every exposed action has a real handler.

E5–E6: rights-denied/unknown assets, corrupted images/media, unavailable fonts,
source transforms, no out-of-bounds trim, frame rounding, overlay positioning,
caption overlap, repeated footage and unknown manifest versions. Render short real
fixtures for visual inspection at 16:9 and mobile viewing size; generated JSON alone
does not establish visual correctness.

E7–E8: authenticated browser journey from brief to preview/private upload; kill and
restart worker/API midway; reconnect without duplicate projects/calls/uploads;
restore persisted state and finish from checkpoint. Use one explicitly authorized
public trailer and actually retrieved research; record source URLs, coverage,
provider receipts, elapsed time, cost, reviewer edits and final output. Separate
synthetic test success from live provider/YouTube acceptance.

Roll out per channel behind capability flags. Database migration first, workers
second, API/UI capability exposure last. Roll back exposure without deleting projects
or revisions. Existing short/compilation rendering remains unchanged.

Post-publish measurement: CTR, first-30-second retention, average view duration,
retention drops aligned to editorial beats, corrections, asset rejection rate,
cost per approved minute and operator edit effort. Compare with channel baselines;
feed aggregate evidence into future story/visual selection without rewriting history.

## Completion evidence to maintain

For every milestone record: commit/PR, schema versions, migrations, worker registration,
API/native tool routes, UI journey, tests executed/results, fixture/live distinction,
known blockers and next dependencies. Full completion requires source-to-reviewed-
render execution, recovery proof and publication integration, not just this plan.

### E1 implementation checkpoint

Implemented in branch `feat/editorial-project-foundation`:

- Migration `0050_editorial_projects` and metadata registration for projects/revisions.
- Strict `editorial-draft-v1` schemas with a 1 MB snapshot cap, bounded lists/text,
  time-range checks, duplicate/reference checks and explicit uncertain-claim disclosure.
- Transactional creation/saves with stable request identity, digest mismatch rejection,
  concurrent-write protection, immutable historical snapshots and DomainEvent audit.
- Authenticated channel-scoped create/list/detail/revisions APIs and five native tools.
- API capability responses explicitly identify draft-only storage and lack of autonomous
  analysis/research/generation/rendering. Structural checks do not establish factual truth.

Local validation: 71 tests passed across editorial, command goals, control authorization,
long-form schemas/manifests and migration graph. Repository-wide Ruff passed; source
compileall and git whitespace checks passed. Includes simultaneous-save tests against
SQLite, API-to-storage round trips and native tool-to-API execution with synthetic data.
The additive migration was exercised up/down against disposable SQLite. PostgreSQL
migration execution remains a CI gate; PostgreSQL concurrency and live provider/media
acceptance are not claimed. No UI/worker/render/publication changes shipped in E1.

Next implementation boundary is E2: resolve real managed source/analysis identities,
persist source provenance and coverage, and add durable intake/start/recovery orchestration.

### E2 intake checkpoint

Draft PR: https://github.com/Deveadra/katcha/pull/257. Foundation CI, launcher and
Cloudflare workflows passed for remote commit `6c55ae4`.

Added durable `EditorialRun` records (migration 0051), start/status/resume/cancel APIs,
native tools and longform-worker registration. A periodic reconciler drains persisted
start intent even when the API could not reach Temporal. Attempts fence stale
completions; cancellation preserves artifacts. The workflow uses existing managed
ingest and local-analysis activities on their registered queues without triggering
unbudgeted generic AI/passthrough side effects. Public HTTPS YouTube intake and
channel-owned clip bindings are supported. Source snapshots preserve clip hash,
measured duration, analysis receipt, frame keys, transcript and sampled-frame coverage.
This checkpoint does not claim native-video understanding or frame-level findings.

Local checkpoint: 81 tests passed including interrupted dispatch/reconciliation,
attempt fencing, cancellation, managed-source reuse and unsupported URL rejection.
Repository Ruff, compileall, longform-worker import and whitespace checks passed.
Live media/Temporal acceptance and the remaining research/render stages are pending.


### E3–E4 research and scripting checkpoint

Continued from merged PR #257 / main `47d8a80`, retaining the reviewed workflow
recovery contract. `target=script` now connects source intake to observation,
specialist question planning, grounded discovery, HTTPS retrieval, exact excerpt
extraction, claim verification and a writer/critic loop with at most two revisions.
The existing start/status/resume/cancel API and native tool expose this target.
The registered longform-worker activity persists progress and actionable blockers.

Evidence snippets have immutable content-derived IDs: another question about the
same page cannot replace an earlier claim's quotation. Questions, documents,
observations, sources, claims, depth, calls, reserved tokens and elapsed time are
bounded. Retrieval pins a validated public IP while retaining TLS hostname checks;
redirects are revalidated and response bytes are limited. Ungrounded model URLs
never become retrieval targets. Script completion and immutable revision saving
share one transaction, fenced against cancellation and concurrent draft edits.

Provider calls reserve durable receipts before dispatch. Validated results are
reused without requiring the provider to remain connected. Ambiguous calls are
blocked rather than resubmitted; a typed Gemini quota rejection may retry only
on an explicit new run attempt. No automatic paid fallback exists. Gemini requires
both `KATCHA_EDITORIAL_GEMINI_BILLING_MODE=free` and an operator-selected
`KATCHA_EDITORIAL_GEMINI_MODEL`, in addition to the key/live execution setting.
This is an operator declaration, not independent verification of account billing.
Otherwise supported connected subscription routes are used. Native Gemini video
input and subscription contact sheets retain different coverage labels.

Validation: **102 focused tests passed** across research, projects/runs, native
tools, control authorization, long-form contracts, migration graph and recovery
registry. Includes injected failure before final commit, cancelled/stale promotion,
full resume without repeating research, immutable excerpts across two questions,
unsupported quotes, critic exhaustion, SSRF/redirect rejection, quota rejection,
call/token budgets and ambiguous requests. Whole-repository Ruff and source
compilation passed. These tests use synthetic provider responses, not live factual
verification. No new migration was required for E3–E4.

Remaining boundaries: live source/provider acceptance, human evidence review,
supplementary asset acquisition and rights decisions, visual compilation/render,
workspace integration and publication handoff. Output is a reviewable script, not
an approved or publishable video. Unknown subscription transport failures remain
ambiguous; no automatic reconciliation endpoint is claimed. Token reservations
are conservative estimates and provider-reported usage is recorded on completion.

### E7 partial workspace checkpoint

Production now includes an **Editorial projects** tab in the existing shared shell.
Operators can save a brief, start source analysis or research/script work, inspect
source observations and linked evidence, resume blocked work, stop active work and
edit narration/visual direction into a new immutable revision. Provider receipts
and research gaps are available behind a disclosure. Unavailable rendering and
publication capabilities remain explicitly identified.

Briefs and unsaved script text persist per channel in the browser tab. Uncertain
create/start/save requests reuse their identity. Channel switching fences stale
responses. A concurrent newer revision retains the operator's unsaved draft and
requires an explicit discard before loading the latest script; it cannot silently
overwrite either version. Active work refreshes progress without replacing typed
script text. This is the research/script portion of E7, not its publication handoff.

Browser validation: new synthetic editorial journey passed, including failed-create
replay, blocked-run resume, evidence links, failed-save replay, concurrent revision
preservation, channel isolation, keyboard tab navigation and 390px overflow checks.
Existing Production browser tests passed. Desktop/mobile screenshots were inspected.
Local browser downloads for pinned Playwright failed, so these local tests used a
separate Chromium 134 executable without changing repository dependency versions.
The research backend checkpoint `09de036` passed all GitHub CI, launcher and
Cloudflare recovery coordinator workflows. UI changes require their own CI run.
