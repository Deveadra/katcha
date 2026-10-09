# Katcha — Autonomous script-to-video first-cut implementation plan

**Audit date:** 2026-10-08  
**Pinned repository:** `Deveadra/katcha`, `main@3d08e5862bedb972c54e4a6ea6e3566354ec9881`  
**Acceptance workload:** operator-provided VisionQuest trailer + finished commentary script, narration upload or explicitly authorized TTS, and authorized external discovery.  
**Purpose:** Produce a complete, accurate, rights-aware, visually varied private first cut from an existing script, preserving Katcha's already-shipped Editorial Studio, provenance, review, budget and rendering contracts.

> **Evidence classification.** "Implemented" means verified source code / merged PR exists at the pinned commit. "Partial" means some operations exist but the named autonomous result cannot be reached with existing APIs. "Missing" means no corresponding production implementation was identified in the audited Editorial path. This is a static repository audit, not a successful live execution or production health assertion. Do not claim E8 live acceptance has been run solely because its executable script exists.

## 1. What the repo actually implements today

| User requirement | Status on pinned main | Code evidence / important boundary |
| --- | --- | --- |
| 1. Analyze an entire supplied script, identify characters, scenes, visual references, transitions, effects | **Partial** | `project_schemas.py` stores an operator `script_seed` (up to 120,000 characters); `research.py` uses it as *unverified writing intent* in a generated/revised cited script. `ScriptBeat` records role, narration, claims, duration, visual intent. No deterministic import-to-locked-beat analysis preserving every supplied line and structured effect/scene/entity cues. |
| 2. Build an exhaustive asset manifest | **Partial** | `asset_schemas.py` defines `AssetPlan`/`AssetRequest` with beat, claim, medium, query, purpose, fallback; max 30 requests. `assets.py` checkpoints discovery candidates. No complete per-scene, timestamped needed-asset inventory with generated graphic instructions, substitution ranking, completeness or source-span-to-target mapping. |
| 3. Discover video/stills across available providers | **Partial** | `assets.py` uses model-backed grounded web search; `acquisition/youtube_discovery.py` supports YouTube Data API search; `acquisition/web_scout.py` supplies general discovery. Editorial lacks a unified beat-level source registry selecting among approved studio catalogs, owned Clip Library, stock/still providers and other configured adapters, with provider-specific capability/rights checks and semantic ranking. |
| 4. Acquire, validate, dedupe and record rights | **Partial** | `editorial_activities.py` acquires selected supporting video using managed discovery/ingest, hashes and measured clips; `services/editorial_runs.py` explicitly rejects non-video automatic asset selection and limits unbound URLs to HTTPS YouTube videos. `images.py` supports manual permission-attested still uploads and cleared source-frame derivation (PR #329). Latest current rights assessment must permit production use at compile time; discovery/download is *not* clearance. No approved-image URL acquisition path or ranked auto-selection/acquisition loop. |
| 5. Organize a project media library and searchable timeline provenance | **Partial, reusable foundation** | `services/clip_lifecycle.py` contains channel-owned clip discovery, metadata, dedupe/search documents and hot/cold lifecycle; ``EditorialImage`, project runs and storyboard state persist lineage. Missing one canonical project-scoped asset inventory with type, licensing, source time ranges, shot IDs, alternate selections, media quality and readiness across videos, stills and graphics; avoid pretending physical folders are required when managed object storage is authoritative. |
| 6. Match precise footage to narration and construct visual timeline | **Partial** | `direction.py` obtains frame-grounded sampled observations with shot citations; `visual_compiler.py` emits deterministic 30-fps scenes timed by actual per-beat WAV frames when narrated. `visual_schemas.py` currently supports one video for single layout, two for comparison, one visual per script beat, source start, playback rate, freeze, push-in, crop, caption and limited overlays. No multi-shot sequence within one beat, shot-boundary-aware retrieval, speech/word-level edit cues, automatic coverage/variety optimizer or verified end-to-end hands-off timeline building. |
| 7. Generate family trees, comparisons, callouts and other missing graphics | **Mostly missing** | Remotion `editorial-video.jsx` already draws quote cards, uploaded/derived images, side-by-side stills, captions, arrow/circle/highlight overlays and basic fades, with branded v6 manifests. `assets.py` currently records `requires_visual_composition` for `quote`/`diagram` asset requests. No reusable declarative family-tree/timeline/diagram generator, graphic generation receipts, render-safe template registry or animation presets. |
| 8. Assemble/render a finished first draft for review | **Partial** | `visual_compiler.py` → `editorial/render.py` → v6 `renderer/src/editorial-video.jsx` support checked local private rendering; `editorial_reviews.py` requires exact approval; PRs #322, #324, #325 brought branding, native publication and ROI handoff. However `run_schemas.py` explicitly starts separate `analysis/script/assets/acquire_assets/narration/direction/render` targets, and `editorial_workflows.py` branches on exactly one target per run. There is no single orchestrator that repeatedly plans, searches, acquires, substitutes, generates graphics, fills timeline and renders a first cut without operator sequencing. |

**Merged functionality not to rebuild:** direct source upload #301; script seed #298; project clip-library selection #297; staged Studio #295; beat-native monitors #315/#319/#320; durable storyboard #316; AI edit proposals #317; v5 router #321; v6 channel branding #322; native Editorial publication #324; packaging economics #325; source-frame stills #329; E8 **live-acceptance runner** #330. The live runner's existence does not prove an authorized full production was actually executed; it intentionally stops at human script/asset/rights/render review gates.

### Definitive functional blockers

1. **The supplied script is not the timeline authority.** Imported `script_seed` is passed to a writing model that may rewrite it, rather than exactly preserving narration with stable line/beat anchors.
2. **Only a thin asset request plan exists.** Max 30 requests, no complete versioned per-visual manifest or readiness accounting, and non-video/diagram work is deferred.
3. **The asset loop is human-selected and video-only.** Candidates can be found; selection then requires explicit candidate IDs; only YouTube-unbound video URLs pass automatic intake. Rights reviews must remain separate.
4. **No deep searchable visual scene index.** Observed sampled frames can ground direction, but not exhaustive reusable shot boundary/entity/region/video-moment retrieval.
5. **The timeline is one layout per beat.** A 30–90-second beat cannot automatically intercut multiple source shots, reference stills and graphic sections with independent in/out boundaries.
6. **No first-class procedural graphics generator** for relationship maps, family trees, timelines, branded labels, structured comparisons or citation cards beyond current limited layouts.
7. **No resumable top-level `Produce first cut` controller** coordinating all stages and review gates.
8. **No verified full-length VisionQuest acceptance proof.** `scripts/editorial_live_acceptance.py` is a valuable existing manual-gate runner, not evidence that an actual authorized 13–14-minute script was completely assembled from relevant media.

## 2. Architecture and durable contracts

Reuse current: `EditorialProject`/`EditorialRevision`/`EditorialRun`, Temporal activities, current provider receipts and budget reservations, Clip Library and object store, rights assessments, frame evidence, durable Storyboard history/Undo, channel brand, Remotion v6 compiler/renderer, private review and publication. New functionality must be additive and schema-versioned, not a parallel editor.

**New contracts (proposed):**

- `ScriptAnalysisV1`: exact script text digest, ordered text spans, stable beat IDs, narration exactly copied unless operator chooses rewrite, shot/reference/character/entity requests, visual cues, effect cues, quote provenance, time estimates, uncertainty/evidence state. Preserve paragraph and character offsets. Distinguish a claimed scene from a *verified* observation; no invented timecodes.
- `AssetManifestV1`: revision-bound `requirements[]` with requirement ID, beat and text span, kind (`footage/still/illustration/diagram/quote/audio/graphic`), semantic intent, verified source/screen target if available, priority, aspect, source preference, max reuse, fallback types, needed duration and candidacy/readiness state. Requests can exceed the legacy 30-item scout cap by deterministic bounded batches.
- `AssetCandidateV1`: original/landing URL, provider, publisher, title, observed metadata, intended reference, search receipt, explicit download capability, license source/evidence, credit terms, source timestamp/region if verified, quality, perceptual/content hashes, project/channel ownership, unreviewed/blocked/cleared states.
- `SceneIndexV1`: clip SHA and analysis-policy version, scene/shot start/end, sampled visual descriptions, source-frame receipts, entities, OCR, transcript words/audio cues, embeddings/search terms, observation confidence/coverage. Never equate sparse keyframes with continuous frame inspection.
- `TimelinePlanV2`: ordered beat timelines containing *multiple* shot segments, image/graphic segments, trims, rates, freeze, multi-layer graphics, captions, approved transitions and audio/caption alignment; frame indices and source-relative times remain separate.
- `GraphicSpecV1`: bounded declarative templates (`family_tree`, `timeline`, `comparison`, `relationship_map`, `source_card`, `label/callout`) with nodes, cited data, typography, brand version, layout constraints, animation preset, rendered asset hash. Never run model-authored React/JS/HTML or unsafe SVG.
- `ProductionAttemptV1`: parent execution/revision, stage DAG, input digests, run IDs, receipts, cost ceilings, selected replacements, warnings, QA report, approval gates and final render receipt.

Recommended additive migrations start after current `0057_editorial_image_lineage.py` and preserve all prior manifest versions. Content-address large raw analysis and renders in existing object storage; keep queryable identities/indexes in Postgres. Never use generated artwork as factual evidence.

## 3. Implementation slices in dependency order

### P0 — Lock the current baseline and verify feature flags (foundation)

**Work:** Pin reference SHA, inventory current Editorial API and models, inspect active provider capabilities/quotas, map what is genuinely live vs synthetic. Document deploy-local renderer requirement and storage limits. Add a dry-run `can_produce_first_cut` report listing missing providers, source authorization, budgets, renderer readiness and rights status without invoking providers or making purchases. Include cost envelopes for web research, retrieval, TTS, vision, storage, render, egress.

**Deliverables:** capability/status contract, deterministic fixture representing a long trailer commentary script, coverage dashboard, explicit operator-source authorization and no-paid-fallback-by-default.

**Acceptance:** dry run correctly blocks missing tools, cost permission or cleared media; never reports “ready” on fixture receipts as a live authorization; exact existing v1–v6 playback and migrations unaffected.

### P1 — Script-preserving analysis and complete manifest (highest priority)

**Work:** Add `editorial/script_analysis.py` and `editorial/asset_manifest.py` plus strict versioned Pydantic contracts. Parse supplied script into ordered narration spans and visual instructions; use an AI pass only to *propose* entity/scene/effect interpretation while preserving original text. Bind factual passages to existing dossier or explicitly mark unverified. Provide `preserve_verbatim` (default for imported finished scripts) and `rewrite_with_review` modes. Stable IDs derive from canonical input digest + span, with deterministic edit remap/invalidations.

Generate one or more precise required visuals per passage and reconcile transitions/graphics/callbacks; track gaps and fallbacks. Batch planning, not silently drop beyond 30; provide printable manifest with each media requirement's source, timing, status and expected screen contribution. Add UI `Analyze script` / `Generate asset plan` and manual edit/lock controls.

**Acceptance:** 100% of narration text maps to ordered beats, 100% of visual references map to requirements or marked ambiguities, never changes locked narration, rejects fabricated source timestamps and unsupported factual assertions. Changing paragraph N invalidates only dependent manifests/segments. Tested on >30 requirements, >100 short source spans and a 13–14-minute target.

### P2 — Multi-provider discovery, rights-aware media acquisition and project bin

**Work:** Build an `AssetSourceAdapter` interface for existing owned Clip Library, existing YouTube Data API search, grounded official publisher/press pages and explicitly configured licensed media/still providers. Each provider advertises `search`, `metadata`, `acquire_for_review`, `rights_evidence`, quotas, authentication and supported file types. Search owned/reusable assets first; rank candidates by script/shot semantic relevance, source authority, verified timestamps, format, quality, licensing evidence, absence of near-duplicates and retrieval cost. Expand image acquisition to supported adapters; keep arbitrary website pages as *leads*, not downloadable images.

Extend existing acquisition activities for permitted images as well as video, with strict host allowlists / SSRF and redirects defenses, MIME/dimension/file-size probes, SHA-256 and perceptual deduplication, provenance, quality thresholds and idempotent request identities. Store all items in a project-scoped *logical* bin referencing canonical Clip Library / managed images, not copied per-project directories. Track required vs collected/eligible/blocked with replacement choices and citations. Preserve source timestamps (actual observed ranges only).

**Nonnegotiable:** a public YouTube/studio URL, or successful download, is not a reuse license. The existing latest-assessment production eligibility remains render authority. Allow acquisition for *review* only where provider terms permit; unknown rights block production selection, with operator-owned media, licensed alternatives, citation cards and original illustrations as substitutions. Never defeat DRM, logins, paywalls, license restrictions, or rate limits.

**Acceptance:** provider adapters return source/usage receipts, stale or revoked clearance blocks render, same URL changed bytes does not reuse old analysis, cross-channel access blocked, interrupted download resumes without duplicates, image+video dedup and audit receipts verified, unavailable provider generates a persistent replacement task instead of loop/retry storms.

### P3 — Deep video intelligence, semantic search and narration matching

**Work:** Add `editorial/scene_index.py` and `editorial/visual_match.py` reusing `media/preprocess.py`, `ClipFeature`, source monitor and frame inspection. Add bounded shot-boundary detection; shot-level keyframes and OCR; transcript time alignment; optional licensed/authorized visual embeddings/entity and character identification with uncertainty and source evidence. Index by media SHA + model/policy version; search at segment level with hybrid text/semantic scoring, diversity penalty and confidence. Record scene coverage/limitations; request extra frame analysis near selected intervals only when authorized/costed.

For each beat, allocate visual slots along measured narration duration (uploaded WAV or confirmed TTS), select ranked intervals with exact time ranges and crop targets; detect on-screen text collisions, visual mismatch and repeated intervals. Do not claim identity, motion or an event between sampled frames. Add operator “why this shot?” explanation with original source and observed frame timestamps.

**Acceptance:** source change invalidates segment index, every selected interval lies within actual measured duration and is grounded, same snippet cannot be overused unnoticed, silence/speech duration is correctly measured, unmatched slots create meaningful graphics/quote fallback tasks, not filler footage. Use a frozen known-shot test clip with known target timestamps and tolerance.

### P4 — Multi-shot beat timeline and deterministic professional rendering

**Work:** Extend `visual_schemas.py` with versioned shot sequences *within a beat*, then `visual_compiler.py`, Remotion contract, `editorial-video.jsx` and related renderer/media hydration. Keep v1–v6 decoding and historic outputs unchanged; introduce v7 if needed. Timings in integer frames, explicit source in/out, per-segment speed/freeze/crop/layers and bounded transitions. Add audio-to-visual slot coverage, per-shot citations and approvals; existing Storyboard save/Undo/AI proposal operate on this *same* new contract, not duplicated browser state. Controls: per-beat auto-arrange, reorder, swap, trim, regenerate proposal without losing manual overrides.

**Acceptance:** clips may alternate 3+ times within one beat; 100% frame coverage with no black/unaccounted gaps or timeline overflows; source trims, frozen frames and transitions remain frame-correct and rights-checked at compile/render/review; renderer verifies output frame count, duration, stream presence and manifest digest. Introduce long-form concurrency/render budget caps so OCI primary is never assumed capable of heavy renders.

### P5 — Original graphic/illustration engine

**Work:** Add `editorial/graphics.py` with deterministic template specs and a curated renderer-controlled library (`family_tree`, `timeline`, `relationships`, `side_by_side`, `quote_citation`, `callouts`, `lower_third`). Derive actual node/label data only from cited research/known operator input; expose uncertainty and source credits. Brand-apply FORESCENE versioned colors, fonts, watermark, safe zones. For optional image generation use explicit enabled provider, per-request cost limit, negative prompt/accuracy guard and provenance; keep illustrations visibly distinct from real trailer frames. Cache by content/brand/spec digest, no arbitrary generated code.

**Acceptance:** a Vision family-tree request yields a legible source-linked graphic asset and short animation; 1080p safe-area/overflow tests; renderer cannot execute untrusted snippets; regenerated same spec produces identical graphic bytes or an explicit nondeterministic generation receipt. Fallback graphics can fill otherwise unserved timeline slots without misleading viewers.

### P6 — One-click first-cut orchestrator and operator experience

**Work:** Add a parent Temporal `EditorialFirstCutWorkflow` and thin API (`POST /editorial-projects/{id}/produce-first-cut` or channel-scoped equivalent). Reuse existing child activity/run services. DAG: lock script → analysis → manifest → search/rank → rights-aware review acquisition → scene indexing → match/graphics/alternates → narrative sync → v7 compilation → QA → local/private render → human review. Add planning-only mode, per-stage resumability, persisted sub-run IDs/idempotency keys and selective invalidation. Provide human gates ONLY where permission, ambiguous factual accuracy, optional paid TTS, rights attestation, or final video approval actually require a person; the ordinary matching/assembly loop should not need repeated clicking. A stopped/blocked call must surface its exact requirement, provider receipt and suggested remedy. No auto-YouTube publishing.

UI: a single `Produce first draft` action with compact progress, cost/rights/readiness, expandable asset manifest and timeline; editable Storyboard remains authoritative. Resume, replan one beat, replace a source, supply narration, approve an asset/license and rerender only dirty output. Integrate typed AI commands with exact project/beat/revision/workspace version, never a free-form authority bypass.

**Acceptance:** with authorized fixture media and prerecorded WAV, project creates a complete first-cut MP4 from a locked script without any manual candidate placement. Resume after worker crash recovers completed assets and provider receipts without charge/reacquisition; model/provider timeout outcome "unknown" prevents blind paid retry; stale rights/script invalidate promotion. Operator can amend a single beat without restarting unrelated stages.

### P7 — Acceptance, quality, economics and staged launch

**Work:** Expand `scripts/editorial_live_acceptance.py` or add `scripts/visionquest_first_cut_acceptance.py`, retaining current E8 human evidence, rights and exact-render review gates. Provide two distinct tests:

- **Deterministic acceptance:** owned/licensed synthetic footage, measured audio, fake research/vision with known ground truth and an intentionally unavailable external source. Runs every PR: no external provider charges; checks acquisition, gap substitutions, multi-shot editing, v7 render and replay.
- **Live authorized VisionQuest acceptance:** real uploaded official trailer + operator-supplied full script + narration, real enabled/authorized search adapters. Require explicit source and each third-party asset's usage determination. Produce a playable full-length private MP4 or correctly report precisely which rights/provider blockers prevented it. Never label synthetic data as real execution.

**Mandatory evidence report:** project/revision/script SHA, input scene/beat/requirement counts, required vs fulfilled/blocked assets, provider costs and uncertain charges, media hashes/rights assessment versions, source timestamps and observed-frame citations per segment, % rendered duration with valid selected visuals, repeat footage %, QA issues, output duration/fps/audio/size/hash, renderer receipt, final operator review. Preserve readable provenance from MP4 timestamp → beat → original source time and rights decision.

**Quality gates (not a promise of retention/revenue):** 100% script-text coverage; 100% planned timeline frames accounted for; 0 unapproved external media in render; 0 source trim overruns; 0 invented evidence/permissions; visually unmapped segments are explicit editorial blockers or safe, appropriately labeled graphics; provider cost never exceeds authorized cap; final private review requires exact render receipt. Add measurable repetition, asset relevance, visual changes per minute, graphics readability and audio alignment thresholds, calibrated against human reviews. Track generation cost per finished minute and time-to-first-cut alongside later watch time/retention when publishing is separately approved.

## 4. Pull-request sequence and ownership

| PR slice | Scope | Dependency | Required checks |
| --- | --- | --- | --- |
| A | Capability/readiness preflight + script-lock/analysis contracts | Existing Editorial | Pydantic, saved revisions, browser seed import, backwards compatibility |
| B | Exhaustive asset manifest, batch planner, coverage and persistent gap state | A | >30 requests, source-span mapping, change invalidation |
| C | Provider adapters + unified ranked candidate registry | B | fixture adapters, grounding/quotas/SSRF, cost/accounting |
| D | Video/still acquisition + logical project bin, rights and replacement flow | C | dedup/rights revocation/recovery/cross-channel/quality |
| E | Shot index + hybrid semantic retrieval + observed citation receipts | D | known-shot grounded seek, false-scene protection, indexing reuse |
| F | Multi-shot TimelinePlanV2 + v7 renderer + editor Undo/AI proposal support | E | Python/JS parity, visual E2E, historic v1–v6 |
| G | Safe procedural graphics / caption and citation cards | B, F | template safety, branding and geometry/regression rendering |
| H | Parent orchestration, progress & one-click Studio UX | A–G; skeleton may begin earlier | crash/retry receipts, approved gates, browser E2E |
| I | Full-length synthetic then authorized live acceptance, rollout/monitoring | H | media, rendered output, receipts, operator review |

Run existing PR Gate, Python tests, renderer tests, browser/Explorer checks, migration checks and launcher/security smoke gates on the exact PR head, plus new contract and media E2E tests. Prefer coherent vertical PRs; do not merge ahead of dependencies or treat pending checks as passing.

## 5. Monetization and operational constraints

- **Fastest viable first cut:** prioritize locked script + manifest + existing cleared library + automatic shot arrangement + existing Remotion. Source only missing segments; don't burn credits rediscovering known media.
- **Free-first, not fictitious free:** use current provider policy and real usage receipts. Operator authorizes any paid vision/TTS/generation/renderer path. Reserve, reconcile unknown outcomes, and retain budget upper bounds.
- **Rights-sensitive reuse:** original graphics, licensed stock, citations and analyzed owned media should be preferred to copying whole third-party trailers. Editorial analysis/commentary is not automatic copyright permission or guaranteed fair use/Content ID immunity.
- **Infrastructure:** keep OCI primary for control/state and dispatch heavy Remotion jobs only where measured compatible compute is available and approved. Never degrade system availability to fill one render.
- **Human creative control:** audio style, factual accuracy and thumbnails matter for retention; one click means no repetitive mechanical labor, not skipping release-critical human review.

## 6. Definition of done

A channel operator can select an existing project, paste/upload a finished ~13–14-minute commentary script and source trailer, attach measured narration, click **Produce first draft**, and inspect a **complete playable private first-cut MP4**. Katcha autonomously inventories visual requirements, searches configured legitimate sources, acquires eligible material with provenance, selects relevant timecoded sequences, creates original grounded diagrams and graphics when needed, fits footage to narration, avoids obvious visual repetition, and renders. Exceptions show *specific* blockages and viable alternatives. All selected material is production-eligible, all outputs are reviewable/traceable, old Editorial projects still render unchanged, costs and retries are auditable, and publication remains a separate deliberate approval.

**Current conclusion:** foundational features are real; the autonomous planner/acquirer/scene-matcher/graphics/timeline/orchestrator combination above is not yet implemented end-to-end at the audited SHA.
