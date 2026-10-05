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
| E5 | Claim-directed supplementary asset scout and acquisition/rights gate | E3–E4 | Video discovery/acquisition and render-time rights gate implemented; images pending |
| E6 | Visual director, timing, compiler and editorial Remotion composition | E4–E5 | Compiler, local renderer, uploaded/generated narration and private preview implemented; automatic direction/live speech acceptance pending |
| E7 | Integrated editorial workspace, recovery/status and publication handoff | E1–E6 | Workspace, recovery, durable review and historical inspection implemented; publication pending |
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


### E5 supporting asset checkpoint

Added `target=assets` and `target=acquire_assets` to the existing typed run API,
native tool and registered workflow. Asset scouting freezes the current saved
script revision, generates bounded beat/claim-linked visual requests, searches
for supporting media and retains only provider-grounded leads. Quote/diagram
requests and missing media remain visible gaps rather than fabricated assets.

Selected YouTube video leads can be downloaded through the existing managed
acquisition activity. The server requires candidate identities from a completed
scout belonging to this channel, project and exact script revision. It rejects
invented selections and unsupported automatic download targets before dispatch.
The workflow registers/reuses discovery candidates and review-purpose sources,
persists clip/source/hash/duration receipts, and avoids generic AI or passthrough
publication side effects. Capture checks the returned clip against its actual
source binding and channel ownership.

Rights status is read from the latest existing acquisition assessment. Cross-channel
records are not exposed, unknown/legacy material is not treated as cleared, and
scouting/downloading creates no rights assessment or permission. Status snapshots
are explicitly labeled as rights at scout time. A future compiler must recheck
current assessments; these snapshots are not render authorization. Images and
non-YouTube material still require existing manual acquisition paths.

Production now lists supporting media, preserves selected candidates across refresh,
and provides a review-download action with plain-language rights status. Native
and sampled-frame interpretation also now attach their actual observation coverage
and limitations to source snapshots. Run replay normalizes newly introduced optional
defaults, preserving request identity for runs created before an upgrade.

Validation: **110 focused Python tests passed**, repository-wide Ruff and worker
import passed. The synthetic browser journey passed with scouting, selection
persistence, review-download request wiring and visible review-required status,
in addition to its earlier recovery/concurrency/channel/mobile checks. This remains
synthetic acceptance; no live trailer download, source verification or rights
clearance is claimed. Prior UI head `9a8bdb2` passed GitHub CI, launcher and recovery
coordinator checks. The new asset checkpoint awaits its own CI results.

Next boundary: revision-bound asset selection and current-rights checks in the
visual compiler; narration/timing and deterministic editorial rendering; then
review/publication handoff and real authorized source-to-render acceptance.


### E6 compiler/preflight checkpoint (not renderer completion)

Added inert, versioned storyboard and `editorial-render-v1` manifest contracts,
with single-video, comparison and evidence-linked quote layouts; bounded circle,
arrow and highlight annotations; playback, freeze and restrained push-in parameters.
The compiler preserves script order, uncertainty disclosures and claim/asset
lineage, builds contiguous integer-frame scene/caption coverage, checks measured
source bounds, and derives deterministic output identities. Caption-only silent
presentation requires an explicit choice and enforces a reading-speed limit; it
is not a fallback for failed narration.

The authenticated channel-scoped `POST .../{project_id}/storyboard/preflight`
API and `preflight_editorial_storyboard` native tool resolve assets from a
completed acquisition of the exact script revision. Storage keys, media hashes,
measured dimensions/durations and current rights assessments are read server-side.
Revoked clearance, changed media, mismatched revisions and invented evidence/media
references fail validation. The returned manifest still requires editorial review.

Validation: 119 focused Python tests passed, including real API preflight and
revoked-rights rejection, deterministic manifests, source freeze/playback bounds,
quote provenance, annotation validation and all preceding regressions. Ruff,
compilation and whitespace checks passed. Asset checkpoint `f7ac570` passed GitHub
CI, launcher and recovery coordinator validation.

No renderer dispatch, Remotion editorial composition, generated narration, image
acquisition, preview playback or publication handoff is exposed by this checkpoint.
Those remaining E6/E7 boundaries require their own wired implementation and actual
render/media checks; preflight success must never be presented as a finished video.

### E6 local rendering and private preview checkpoint

Accepted PR #258 is merged; all CI, launcher and recovery coordinator checks passed.
The next implementation now connects the versioned editorial manifest to an actual
Remotion composition and local renderer. Single footage, split comparisons, held
frames, push-ins, source-coordinate annotations, evidence quotes, captions and
uncertainty disclosures use deterministic integer-frame timing. The renderer
validates managed media and source bounds before cache lookup or dispatch. Cached
editorial objects require a matching manifest digest and verification receipt.
Editorial Lambda dispatch is explicitly rejected until cost authorization is wired.

`target=render` accepts a revision-bound acquired asset run and explicit
`captioned_silent` storyboard. The registered Temporal activity persists its
manifest and dispatch receipt before requesting compute, fences cancelled/stale
attempts, and rechecks current script/media/clearance before review readiness.
Resume reconciles uncertain dispatches through a read-only renderer output check;
it never starts replacement compute while the previous result is unknown. A
confirmed pre-dispatch validation/configuration rejection can be retried after a
configuration fix. Uncertain work with no output stays blocked for inspection.

Production now provides beat-level footage/evidence selection, source start,
freeze choice, persisted unsaved storyboard choices, explicit silent-preview
creation, progress/resume and authenticated private preview loading. Preflight is
performed before submission; script edits must be saved first. Playback again
validates channel, project, current revision, current rights and verified output.
The renderer completion receipt is not publication approval. Existing acquisition
receipts remain discoverable after a render becomes the latest project run.

Validation includes durable lost-response recovery, cancellation, rights revocation
before promotion and during preview access, same-receipt dispatch exclusion, and
configuration rejection recovery. The synthetic browser journey covers storyboard
persistence, preflight/render request identity and authenticated preview transport,
plus existing mobile, keyboard, script recovery and channel isolation checks.
Actual Remotion/FFmpeg acceptance produced a 1920x1080, 180-frame, silent MP4 with
single, comparison/freeze and quote scenes; representative frames were inspected.
This is synthetic rendering proof, not live trailer/provider/rights acceptance.

Remaining: measured narration and voice policy; automatic visual direction grounded
in asset frames; approved brand assets; richer visual editing and historical run
selection; image acquisition/manual replacement; review/publication handoff; and
real authorized source-to-reviewed-video operational acceptance. The current UI
uses single footage and quote cards; comparison/annotation plans are available
through the typed storyboard API. No paid renderer fallback or publication action
is introduced by this checkpoint.


### E7 durable render review checkpoint (local, not published)

Added migration `0052_editorial_render_reviews` with append-only, uniquely sequenced
render decisions. Reviews bind both the exact manifest and verified output receipt,
store the authenticated actor, and emit a transactional domain event. Project-row
serialization shares the draft-save lock: simultaneous reviews cannot overwrite
each other, and an interrupted response can replay the original decision exactly.
A replay is a historical receipt, not proof that an old approval remains valid.

Authenticated review/status APIs, native tools and Production controls expose
approve/request-changes, notes, recent history and invalidation reasons. Native
AI review requires explicit operator confirmation. Notes and request identity survive
failed saves. Approval rechecks the current revision, media and rights; subsequent
status checks invalidate approval when inputs or output receipts change. Preview
access now also verifies measured duration and composition against the manifest.
Rights/compiler reads can share the review transaction instead of opening nested
sessions inside it. Requesting changes remains possible after clearance revocation.

Validation: 134 focused Python tests passed across editorial stages, review replay,
concurrent reviewers, API/channel scopes, native schemas, exact output checks, real
compiler rights-revocation integration, migration upgrade/downgrade and existing
long-form/recovery contracts. Renderer configuration and editorial geometry contract
tests passed. The browser journey now includes review notes, approval and lost-response
replay, but execution is blocked in this session: Chromium is absent and both browser
downloads returned unusable archives. JavaScript syntax checks passed; no new browser
or live source/provider/YouTube acceptance is claimed. Full-suite execution against a fresh disposable SQLite database reached **940 passed,
2 skipped and 1 failed**. The source-scout recovery failure reproduced unchanged on
the starting renderer commit `9f846c5`; it is not introduced by this review change.
An initial run without a database had 18 connection-refused failures. PostgreSQL
migration execution remains a CI gate; SQLite results do not establish it.

GitHub publication was blocked by automatic approval review, which said explicit
publication authorization was missing despite the authorization in the supplied chat
history. Renderer commit `9f846c5` and this review follow-up remain local until that
block is resolved. No remote PR or CI result is claimed for these changes.

Still pending: measured narration and voice policy, automatic visual direction,
image/manual-replacement support, approved brand assets, historical run selection,
publication-source integration and real operational source-to-reviewed-video acceptance.
Review approval does not upload, schedule, publish or authorize paid provider calls.

### E8 measured uploaded narration checkpoint

The renderer/review checkpoint above is now merged in PR #259 at `44e05be`.
All four remote workflows passed, including the PostgreSQL CI gate. The earlier
publication and browser-download blockers are resolved.

Added revision-bound, permission-attested PCM WAV intake, measured sample timing,
replay-safe uploads, removal with retained history, and channel voice-off checks.
Migration `0053_editorial_narration` records immutable object/hash receipts and
transactional audit events. Uploads happen outside the database transaction, then
revalidate the saved script under the project lock. A failed commit can leave an
unreferenced object; automatic orphan cleanup remains pending.

Narrated version-2 manifests use measured audio lengths for scene/caption timing,
verify bounded audio bytes against their checksum before rendering, and require an
audio stream in the rendered receipt. Version-1 silent serialization is preserved
so existing frozen manifests and approvals retain their hashes. Removing narration
invalidates current approval through the existing compiler-backed review checks.
Production supports per-beat recording selection/upload/removal, retry after an
uncertain response, and narrated preview creation. Native tools expose recordings.

Validation: 148 focused Python tests passed, including migration roundtrip,
revision/channel/voice policy, replay, storage failure and review invalidation.
Renderer contract/configuration checks and lint passed. A real local Chromium /
Remotion / FFmpeg render produced a 1920x1080, 180-frame narrated MP4 with PCM
fixture audio; silent footage, comparison, freeze and quote scenes also passed.
The browser journey passed upload failure/retry, selection, narrated render request,
existing review recovery, channel isolation and 390px layout. Synthetic fixtures
and mocked API journeys do not establish live-provider operational acceptance.

This checkpoint accepts operator recordings; automatic TTS remains pending until
billing ambiguity and voice-profile recovery are handled. Recording text is not
transcribed or verified, and caption timing is proportional, not word-aligned.
Remaining: generated narration, automatic visual direction, image/manual replacement,
approved branding, historical run selection, publication handoff and real authorized
source-to-reviewed-video acceptance.

### E9 recoverable work-history inspection

PR #267 (measured uploaded narration) merged at `c9834ba`; all five remote
workflows passed, including CI, launcher, runtime images and both Cloudflare checks.

Production now has a separate, lazy-loaded Project work history inspector. It lists
prior work with stage/status, input revision, attempt and timestamp, and loads the
exact saved output script (or input revision), cited evidence, diagnostic receipts,
review history and private preview. Inspection leaves the working script, narration
selection and current review controls untouched. Earlier approval receipts are
shown separately from their current validity. Existing preview clearance checks
remain mandatory; a changed revision can make an old video unavailable.

The run-list API accepts a project-scoped keyset cursor, preserving pagination when
new work starts and handling timestamp ties deterministically. Existing offset
clients remain supported. An exact immutable-revision read endpoint is available
to both the operator and native AI tools. History requests and media URLs are fenced
by channel/project identity; late responses cannot restore the previous channel's
content. Failed pagination retains the last successful page, and failed inspection
or playback exposes retry instructions. History is read-only; it does not restart,
approve, restore a revision or publish a historical result.

Validation: 108 focused Python tests passed across history, project/run/review,
narration, native tools and authentication. Browser fixtures passed historical
pagination and interrupted-page retry, exact saved script display, invalidated
review display, refused/retried preview transport, preservation of unsaved edits,
and switching channels during an outstanding history request. These are synthetic
API fixtures, not live-provider or playback-content acceptance. The existing
narration browser test now waits for its actual POST response instead of matching
an approval message left over from the prior render.

Remaining: generated narration with billing recovery, automatic visual direction,
image/manual replacement, approved branding, publication handoff and authorized
operational source-to-reviewed-video acceptance. History inspection does not make
historical previews exempt from current clearance or allow restoring old revisions.

### E10 generated narration and verified billing recovery

PR #268 is merged; its CI, launcher and recovery workflows passed. This checkpoint
is integrated with main through `a37f264` and preserves the historical inspector.

A durable `narration` run generates each saved script beat using the channel's
configured ElevenLabs primary long-form voice. It requires explicit confirmation,
live channel policy, voice enabled, a known configured credit cost, and an operator
estimate ceiling. Voice/model/output format/rate are frozen on the run. The ceiling
is an estimate using that configured rate, not a guarantee of the provider's final
charge. Channel and global budget checks still apply. Editorial project usage is now
included in channel spending instead of disappearing after reservation settlement.

Each speech call atomically claims a non-expiring dispatched reservation. Timeouts,
invalid output and uncertain storage outcomes retain the hold and block automatic
regeneration. Explicit provider rejections permit retry. A successful response is
validated as bounded PCM WAV, checksummed, stored privately, and given a durable
receipt before accounting and narration attachment. Retries recover those bytes;
completed beats are reused. Dispatch counters fence late responses and stale billing
confirmations. Receipt storage, accounting and reconciliation serialize on the ledger.

The operator can reconcile an unknown charge from Production after checking the
provider's final outcome. A receipt/reference and explicit confirmation are required;
charged outcomes create usage once, and confirmed no-charge outcomes permit another
attempt. Replays return the original audit receipt without changing a newer attempt.
The API is channel-scoped and the native reconciliation tool requires confirmation.
Katcha records the operator's verification; it does not independently query the
provider's billing history. Confirmed charges without recovered audio remain blocked
from automatic regeneration; recovered recordings may be uploaded for review.

Shared channel-budgeted TTS also retains unknown holds and blocks repeated dispatch.
OpenAI/Gemini speech SDK automatic retries are disabled; provider error text alone is
not sufficient to release a reservation or fail over. The new reconciliation UI is
specific to editorial runs; non-editorial unknown speech holds still require
operational reconciliation. No migration is needed: existing reservation metadata
stores the immutable voice/audio/reconciliation receipts and dispatch counter.

Validation: full local SQLite suite reached **1049 passed, 2 skipped, 1 failed**.
The source-scout cycle-recovery failure is intermittent on unchanged main `a37f264`:
that baseline passed its complete suite (1026 passed, 2 skipped), then reproduced
the same failure on an isolated repeat. No source-scout implementation was changed.
Generated narration tests cover saved-audio/settlement/attachment recovery, voice and
revision gates, explicit rejection, unknown charges, stale callbacks, channel scopes,
confirmation, accounting replay, completed-beat reuse and workflow registration.
The editorial browser journey, existing Production and shared Aerith checks passed.
Generation and reconciliation preserve request identity and input after a lost
response; the active billing form was visually checked at 390px. Ruff and compilation
passed. These tests use synthetic provider/audio fixtures; no live provider call or
paid narration acceptance is claimed. PostgreSQL/full remote CI remains a gate.

Still pending: automatic visual direction, image/manual replacement, approved brand
assets, publication handoff, provider-specific live speech acceptance, broader
non-editorial billing-reconciliation UI and authorized source-to-reviewed-video
operational acceptance. Captions remain proportional rather than word-aligned.

### E11 recoverable semantic visual direction

PR #275 merged at `20631e7`; CI, browser checks and the launcher passed.

A durable `direction` target now plans acquired footage, comparisons, freezes,
restrained push-ins and linked evidence quote cards for the current script. The
operator explicitly selects silent captions or exact narration recordings before
planning. Narrated timing uses measured sample lengths rounded to render frames.
The existing bounded research-provider gateway persists responses, reuses completed
receipts and blocks automatic repetition of uncertain requests; no paid fallback
is introduced. Cancellation and replaced attempts cannot promote model output.

The server resolves candidate identities and measured lengths from completed,
revision-bound acquisition. The compiler validates beat/claim links, quote sources,
current rights, media identity, playback bounds and narration before saving a usable
storyboard. Invalid proposals remain available when structurally valid, without
starting a render. Review notes flag long unchanged scenes and repeated source
intervals; these are heuristics, not retention predictions. Comparisons require
distinct candidate identities.

Production exposes Plan visuals, per-beat reasons and timing choices, warnings, and
an explicit Create preview from this plan action. The saved plan retains its exact
presentation and recording selections. Render preflight rechecks current clearance.
Manual storyboard entries remain independent and survive planning/reconnection.
Native Katcha AI can use the same typed stage. No migration is required.

This first director is text-based: it reads acquired asset descriptions and the
saved evidence dossier, not the acquired footage itself. It cannot establish object
coordinates, so automatic spatial overlays are rejected. It does not approve rights,
render review or publication. Frame-grounded direction, image/manual replacement,
approved branding, publication handoff and live source-to-reviewed-video acceptance
remain pending. No live provider or visual-quality acceptance is claimed.

Validation: **1070 passed, 2 skipped, 1 failed** in the full local SQLite suite.
The failure is the same source-scout cycle recovery intermittence previously
reproduced on unchanged main during E10. All 21 new direction cases passed,
including measured narration, removed recordings, interrupted-provider recovery,
real compiler clearance/hash/bounds checks, stale revisions and cancellation.
The editorial browser journey passed lost direction-response replay, narration
selection, exact saved-plan rendering, preservation of manual edits, and 390px
layout. Ruff, compilation, JavaScript syntax and diff checks passed. PostgreSQL and
remote CI remain publication gates; fixtures do not establish live visual quality.

### E12 permission-attested uploaded still images

PR #276 is merged. This milestone is integrated with main through `0c2b9fa`.

Production now accepts PNG/JPEG stills assigned to exact saved script beats. Uploads
require an on-screen credit/title, source or ownership reference, permitted-use basis
and explicit permission attestation. Generated artwork can be marked as an illustration;
that label is rendered on screen. This records operator authority, not independent
rights clearance or verification that the image depicts a factual event.

Intake bounds bytes, dimensions and decoded pixels, rejects animation and unsupported
formats, applies EXIF orientation, strips metadata and stores a normalized PNG with
an immutable checksum receipt. Upload replay retains identity after a lost response;
changed bytes or permission details conflict. Persistence rechecks the current script
after storage. Removal is audited and retained in history; it invalidates current
render approval through the compiler's existing review checks. An interrupted database
commit can leave an unreferenced object; automatic orphan cleanup remains pending.
Migration `0054_editorial_images` is additive and follows narration migration 0053.

Version-3 manifests can mix uploaded image scenes with acquired video and quote cards,
with silent captions or measured narration. Image/quote-only previews do not require
a video acquisition run. Images keep their measured aspect ratio; native storyboards
can request restrained push-in. The renderer checks managed image keys, bounded bytes,
checksums and PNG dimensions before rendering. Versions 1 and 2 retain their serialized
shape, and the direction provider schema remains exactly compatible with saved receipts.
The text-only director still requires manual selection of uploaded images.

Validation: **1083 passed, 2 skipped, 1 failed** in the full local SQLite suite. The
failure is the same pre-existing source-scout cycle-recovery intermittence recorded in
E10/E11. All 13 new image cases passed, including migration roundtrip, authentication,
replay, permission gates, storage failure, script races and approval invalidation.
The browser journey passed interrupted upload/replay with retained file and permission
fields, per-beat image selection, narrated render submission, removal and 390px layout.
A real Chromium/Remotion/FFmpeg render produced a 60-frame image video with narration
and an illustration credit; existing 180-frame silent and narrated renders also passed.
The rendered still and mobile form were visually inspected. Local render acceptance
used a test-only loopback-interface fallback because interface enumeration is unavailable
in this execution environment; application and renderer code were not altered for it.
Ruff, compilation, renderer contracts, JavaScript syntax and diff checks passed.

Remaining: frame-grounded visual direction, automatic image acquisition, image comparison
and region annotations, approved channel branding, publication handoff and live authorized
source-to-reviewed-video acceptance. This milestone does not generate artwork or publish.
