# Katcha Source Intelligence & Content Intake — Two-Agent Implementation Plan

**Prepared:** 2026-10-10  
**Repository:** [`Deveadra/katcha`](https://github.com/Deveadra/katcha)  
**Evidence baseline:** accepted `main@3d08e5862bedb972c54e4a6ea6e3566354ec9881` (verified 2026-10-10). This plan is a proposal; no runtime tests, production rollout, or live provider acceptance are claimed.  
**Related unmerged work:** draft [PR #345](https://github.com/Deveadra/katcha/pull/345), overall autonomous first-cut implementation plan; draft [PR #346](https://github.com/Deveadra/katcha/pull/346), script-to-visual inventory. Reconcile against those PRs before writing code, and never treat draft code as accepted `main`.

## Executive decision

**Highest priority: make Katcha genuinely find, evaluate, collect, and organize the material needed to create a video from a supplied script.** The deliverable is not a search suggestion list, a downloader button, or a bot that indiscriminately scrapes sites. It is a project-scoped *source intelligence and intake system*: an AI-led, bounded worker fleet that follows each script requirement from research through an auditable, usable media or reference record, suitable for human inspection and subsequent automatic editing.

**Defer** unrelated channel features, interface restyling, additional publishing automation, fancy motion graphics, and general infrastructure work that is not necessary to safely operate this pathway. Keep existing production, media, recovery, billing, authentication, and Editorial contracts intact.

### Non-negotiable user journey

1. Operator opens an Editorial project, pastes/uploads the *finished* script, supplies any existing video and/or narration, and selects **Find & prepare media**. The input text is not silently rewritten.
2. Katcha parses the entire script into stable spans/beats and a comprehensive inventory of required visuals, entities, scene references, quotes, timelines, audio, and alternative treatments. Ambiguities remain explicit rather than fabricated.
3. An orchestrator dispatches bounded specialist workers to search the existing Clip Library, trusted official sources, licensed stock/still catalogs, and approved searchable websites. Workers intelligently expand queries and pursue semantic alternatives, not merely match literal script phrases.
4. Source verifier gathers true publisher identity, media metadata, provenance, duplicate relationships, relevance evidence, license/usage evidence and confidence. Source references are saved even when downloading is unavailable or prohibited.
5. Intake workers obtain **only provider-permitted / operator-authorized** originals; validate file types and quality; deduplicate; preserve attribution, permitted-use scope, timestamps and rights receipts. Unknown, blocked or reference-only items stay useful as research leads, not video assets selected for a render.
6. Scene-index workers inspect authorized media into shots, frame evidence, searchable tags, OCR/transcript cues and measured timecodes; then map candidate intervals to script requirements. AI cannot invent exact source timecodes or assert unseen events.
7. The project **Media Bin** shows what is downloaded, linked, verified, blocked, missing, or ready. The user can preview, manually replace, tag, approve, export, or use each asset. A portable export produces physical media and `links.csv` / `manifest.json` for manual editing; durable cloud object storage remains canonical.
8. An **editing handoff** exposes stable, rights-checked, timecoded asset bindings so Katcha can later select, trim, and assemble footage automatically using its existing Editorial/Remotion systems. Full first-cut automation follows in the broader PR #345 plan; the acquisition engine must not wait for that renderer work.

### What counts as a *smart operation*

- **Break down intent:** distinguish “show Ultron at the end of *Age of Ultron*” from “explain why that ending left fans unconvinced.” The former calls for a specific verified moment; the latter may be supported by licensed/owned frames, source citations, commentary graphics, or multiple references.
- **Research across source classes:** first query owned/reusable assets, then official publisher/press pages and approved licensed libraries, then grounded public link discovery. Never confuse search-result snippets with actual media observations.
- **Delegate** narrow tasks to specialist jobs (`research_scout`, `metadata_verifier`, `rights_verifier`, `media_collector`, `shot_indexer`, `coverage_curator`). Each has explicit limits and tool permissions. An LLM proposes and scores; deterministic services enforce downloads, permissions, data mutations and budgets.
- **Iterate intelligently:** if a source is irrelevant or only referenceable, reformulate queries, widen to permitted alternates, or generate a *specific* unresolved gap. Never retry indefinitely, invent a URL, turn a search hit into “cleared footage,” or silently substitute generic stock for a unique scene.
- **Prove coverage:** every script requirement ends `fulfilled`, `reference_only`, `blocked`, `needs_operator_input`, or `unresolved`, with evidence and an actionable next step. Never claim “all assets acquired” because links were found.

## 1. Repository-grounded assessment

| Existing accepted foundation | Evidence on `main` | Remaining shortfall for this goal |
|---|---|---|
| Editorial source upload | `src/katcha/editorial/source_uploads.py`; video probing, hashing, Clip Library storage | No complete multi-source autonomous still/video/reference collector |
| Generic discovery adapters | `src/katcha/acquisition/adapters.py`, `youtube_discovery.py`, `web_scout.py`, `reddit_discovery.py`, `feeds.py` | These are discovery adapters, **not** a unified Editorial requirements-to-production-asset service |
| Script research and asset scouting | `src/katcha/editorial/research.py`, `assets.py`, `asset_schemas.py` | Script seed may be rewritten downstream; legacy asset requests have a bounded 30-item model and quote/diagram gaps; no complete persistent per-requirement planning loop |
| Existing video acquisition workflow | `src/katcha/orchestration/editorial_activities.py`, `src/katcha/services/editorial_runs.py` | Selected candidate pathway is constrained and largely video-oriented; not automated universal selection/authorized still intake |
| Clip Library / storage | `src/katcha/api/clip_library.py`, `src/katcha/services/clip_lifecycle.py`, `src/katcha/integrations/storage.py` | No canonical project-scoped bin unifying downloaded files, unacquired URLs, rights receipts, shot indexes and requirement bindings |
| Rights/review models | `src/katcha/acquisition_models.py`, `src/katcha/acquisition/eligibility.py`, Editorial review code | Must explicitly gate source-specific access and use; `production_eligible_clip_ids` currently allows legacy unmanaged clips—preserve compatibility but never let that fallback silently clear *new* external intake |
| Visual analysis and current renderer | `src/katcha/editorial/direction.py`, `visual_compiler.py`, `renderer/src/editorial-video.jsx` | Sampled-frame observations and a predominantly one-layout-per-beat editorial model, not a deep reusable shot search + multi-shot timeline |
| Existing coordination plan | draft PR #345 | Broad P0–P7 pathway, not yet accepted; this document expands and prioritizes source intelligence/intake |
| Script inventory work in progress | draft PR #346 | Treat read-only exact-span/cue analysis as an integration dependency, not as a merged capability |

**Status:** static repository findings, not verified live behavior. Re-read accepted `main`, open PRs, schema/migration heads, and running tests at the start of every implementation slice.

## 2. Scope boundaries and safety invariants

1. **Discovery, access, download, and production reuse are separate decisions.** A public URL or successful HTTP response is never itself a license. A found YouTube trailer is usually a **link/reference**, not a file the YouTube API authorizes downloading. Official YouTube API policies prohibit API clients from downloading/caching audiovisual material absent approval and prohibit scraping YouTube applications; do not implement a downloader/scraper that bypasses those terms. The existence of an existing `yt-dlp` dependency is not download authorization. Sources: https://developers.google.com/youtube/terms/developer-policies and https://www.youtube.com/t/terms.
2. Acquire direct media through **owned/operator-supplied files, expressly licensed catalogs, publisher-provided authorized downloadable files, or a documented rights/permission path**. Include attribution, license snapshot/evidence, usage purpose, territory, expiry, change/revocation, and evidence timestamp where applicable. License terms are provider-specific; no blanket clearance assumption.
3. Scrape only when site terms, permissions, robots directives and applicable law permit that method. Prefer documented APIs, public feeds, and official press/download pages. Do not bypass paywalls, account gates, DRM, throttles or anti-bot mechanisms.
4. Non-downloadable media remains discoverable through links, publisher metadata and operator action. Never fabricate a local file, preview, timecode, or download capability.
5. Every network fetch is allowlisted/capability-checked and protected against SSRF, private IP/DNS rebinding, redirect host changes, oversized responses, decompression bombs, dangerous content, MIME/extension mismatches and malicious archives.
6. Default paid activity off unless authorized at project/provider level; bound calls, bytes, retries, tokens, time, parallelism, storage and egress. Keep provider usage and unknown-charge receipts. A provider timeout after a potentially billable request does **not** permit blind retry.
7. Channel and project access must be enforced on reads and writes. Respect existing authority models, no cross-channel accidental sharing, no production secrets in logs or artifacts.
8. Media hashes and object keys are immutable for a given content version. Changed source bytes trigger new analysis and rights checks. Dedup cannot lose original source lineage.
9. Review rights before acquisition when required; always recheck applicable production eligibility at clip binding/render time. Do not weaken existing review or publishing gates.
10. Retain accepted old projects, current APIs, historical Editorial render contracts, recovery guarantees, and OCI resource safety. No direct `main` pushes, CI bypass, forced history operations, auto-merges or auto-deploys.

## 3. Proposed architecture: one coordinator, bounded specialist workers

```text
Editorial script / user uploads / narration
             |
       ScriptVisualPlanV1  [PR #346; if accepted]
             |
      AssetManifestV1 / requirement coverage
             |
      SourceIntelligenceCoordinator
      /           |              \
 Owned bin   Official/API scouts   Licensed catalog scouts
      \           |              /
      Grounded candidate registry + evidence
             |
      Candidate ranker / gap tracker
             |
        Capability + rights gate
             |
  +----------+-----------+----------------+
  |                      |                |
Reference-only       Permitted          Rejected/
(URL + evidence)     acquisition         blocked
  |                      |                |
  |             Validated object store    |
  |               + hashes / previews    |
  |                      |                |
  +--------------- Project Media Bin -----+
                         |
                   Shot-level index
                         |
                Requirement-to-shot bindings
                         |
           Existing Storyboard / later first-cut controller
```

The specialist “bots” are **typed Temporal activities/child workflows backed by AI where useful**, not unlimited shell agents. The coordinator plans searches and dispatches jobs, keeps every job idempotent, checks provider budgets and policy, reconciles outcomes, and stops when requirements are covered or each gap has an explicit reason. No autonomous bot can grant rights or run arbitrary generated scripts.

### Worker responsibilities

| Worker | Input | Allowed output/actions | Forbidden behavior |
|---|---|---|---|
| `requirement_planner` | Exact script spans, project revision, narration measurements | Versioned requirements, aliases, search intents, evidence gaps | Rewriting locked script or inventing source timestamps |
| `source_scout` | Requirement + configured provider capability + budget | Grounded URLs/media identifiers, alternate queries, query receipts | Unbounded crawling; downloaded media claims |
| `metadata_verifier` | Lead URL/provider ID | Publisher, title, type, available formats, dimensions, source link, timestamps only if verified | Guessing URLs, making rights determinations from existence |
| `rights_verifier` | Provider/license evidence, operator attestations | Scoped rights assessment, allowed operations and expiry, evidence | Setting clearance because an AI says “probably fair use” |
| `media_collector` | Permitted download capability, stable asset ID, bounded budget | Validated file, hash, object pointer, retry/partial state, ingest receipt | Accessing unapproved URLs, DRM bypass, production use decision |
| `scene_indexer` | Ingested authorized media SHA and analysis policy | Shot intervals, actual frame/OCR/speech observations, metadata indexes | Asserting unseen events or unmeasured timecodes |
| `coverage_curator` | Manifest, candidates, rights, scene indexes | Ranked bindings, fallbacks, gap tasks, why-this-source evidence | Quietly replacing a scene-specific clip with generic filler |

**Dispatch rules:** start with owned assets, prefer official/authorized evidence, use at most a configured number of parallel scouting jobs per requirement and per provider, respect provider rate headers, make progress persistent after every candidate/page, stop on policy blocks, and provide visible `not_found` rather than silent completion. Worker success is measured by verified coverage, not URL volume.

## 4. Additive data contracts (freeze before parallel edits)

Reuse existing `EditorialProject`, `EditorialRevision`, `EditorialRun`, `Clip`, `SourceItem`, `DiscoveryCandidate`, `RightsAssessment`, `RightsEvidence`, `ClipFeature`, Temporal jobs and ObjectStore. Add schema-versioned records through additive migrations and stable IDs. Do not create a separate competing global Clip Library.

**`AssetRequirementV1`:** `requirement_id`, `project_id`, `revision_digest`, `script_span_id`, `beat_id`, `intent`, `kind` (footage, still, sound, music, diagram, graphic, citation), `entities`, `exact_scene_if_verified`, `minimum_needed_seconds`, `relative_narration_slot`, `aspect`, `importance`, `approved_source_classes`, `alternatives`, `lock_status`, `coverage_status`, `gap_reason`. A requirement covers *one desired visual purpose*; a beat may have several requirements and >30 requirements per project are routine.

**`SourceSearchJobV1`:** `job_id`, `requirement_id`, `provider_key/version`, `query_terms`, `negative_terms`, `semantic_brief`, `source_purpose`, `cursor`, `max_results`, `max_cost`, `attempt`, `requested_by`, `policy_version`, `status`, `receipt_refs`. `query_terms` are bounded and logged; variants derive from entities, scene titles, exact descriptions, date/publisher hints and acceptable alternate representations.

**`AssetCandidateV1`:** `candidate_id`, `requirement_ids[]`, `original_url`, `canonical_url`, `provider`, `provider_media_id`, `publisher`, `source_type`, `discovered_at`, `retrieved_at`, `observed_metadata`, `provenance_evidence_refs`, `availability` (`reference_only`, `direct_media_capability`, `owned`, `unavailable`), `license_evidence_refs`, `rights_state`, `quality`, `scout_receipts`, `relevance_features`, `alternatives`, `content_hash_if_obtained`. All conclusions are evidence-linked, and incomplete fields remain null.

**`AcquisitionReceiptV1`:** `candidate_id`, `operation_key`, `authorization/evidence_scope`, `fetch_url_if_allowed`, `status`, `request_time`, `response_time`, `bytes`, `sha256`, `mime`, `ffprobe/image_dimensions`, `object_store_key`, `duplicate_of`, `source_version`, `download_terms`, `cost_receipt`, `error_category`, `safe_resume_cursor`. Durable state distinguishes **provider outcome unknown** from known failed and known succeeded.

**`ProjectMediaItemV1`:** `project_id`, `channel_id`, `asset_id`, `requirements[]`, `kind`, `bin_path`, `physical_object_ref` (optional), `external_reference_url` (optional), `thumbnail_ref` (only if permitted/available), `selected_state`, `rights_assessment_id/version`, `searchable_tags`, `source_in/out` (only measured), `notes`, `provenance_refs`, `created_at`, `last_verified_at`. One canonical item may be linked by multiple project-specific references without duplicating bytes.

**`SceneIndexV1`:** `clip_sha256`, `algorithm/model/policy_version`, `duration_frames/timebase`, shot boundaries, observed frames and times, confidence, speech transcript segments, text/OCR evidence, entity tags, source lineages, search vectors/terms, indexing receipts. If only sparse samples are available, label the coverage accordingly.

**`RequirementBindingV1`:** requirement ID → candidate/media item ID → optional observed shot interval → usage eligibility version → confidence/reason → approved or proposed. This is the contract for later automatic editing; selected assets must be revalidated before render.

**Reference state:** `discovered → metadata_checked → (reference_only | acquisition_candidate | rejected)`; acquisition branch `authorized → acquiring → acquired → validated → indexed`; rights can independently be `not_assessed | reference_only | review_permitted | production_cleared | restricted | denied | revoked | expired`. Production selection is allowed only if current policy/evidence explicitly authorizes it. Every state transition gets a durable receipt and reason.

**One authoritative project package:** files live in the existing managed object store; logical bins join files and links. Offer **Export project media** with a user-owned physical folder/archive:

```text
<project-slug>/
  script/original-script.md
  manifests/requirements.json
  manifests/candidates.json
  manifests/bindings.json
  media/video/<hash>.<ext>           # only permitted acquired media
  media/images/<hash>.<ext>          # only permitted acquired media
  media/audio/<hash>.<ext>           # only permitted acquired media
  references/links.csv              # non-downloadable references retained
  evidence/rights-and-sources.json
  analysis/shot-index.jsonl
  README.md                         # provenance, gaps, restrictions, import guide
```

Export the source SHA, origin URLs, rights evidence and timestamps with each asset; never export secrets or transient signed URLs as permanent references. Offline export is a convenience—not a second source of truth or a requirement to store duplicate media inside a container filesystem.

## 5. Search-and-rank intelligence

For each visual requirement:

1. **Understand:** extract distinct entities and disambiguate (character, actor, franchise, scene, episode, temporal period, action, visual treatment). Preserve which details came from the script vs research; assert nothing as visually observed until media is inspected.
2. **Plan:** generate bounded, reviewable search hypotheses: exact scene, official publisher clip/still, related interview/behind-the-scenes, legally usable licensed illustration/B-roll, or original graphic fallback. Include synonyms, release titles, character aliases and negative terms.
3. **Discover:** run provider-aware search using existing adapter infrastructure; return actual URLs or provider IDs, ranking receipts and search strategy. A failed provider returns bounded replacement tasks; it does not disable the project.
4. **Verify:** confirm original publisher, page/media relationship, observable metadata, direct downloadable capability, usage restrictions and source integrity. Avoid high-confidence claims from descriptions alone.
5. **Rank:** first apply hard eligibility gates (availability, authorization, accessibility, file validity), then score relevance with a versioned model/feature breakdown. Candidate features: exact visual identity, factual source authority, narrative suitability, verified timecodes, rights certainty, resolution/aspect, quality, duplication penalty, cost, visual variety and provider confidence. Never let an attractive semantic match override hard rights/access checks.
6. **Fill:** save primary candidate plus meaningful alternates; acquire permitted files; index footage; propose shot bindings. If none meets required conditions, record a specific gap and continue to the next requirement.
7. **Iterate:** requery only low-coverage/high-value requirements within quota; prefer approved alternatives and original explanatory graphics when exact footage is unavailable. Avoid repeating the same URL or paid request on replay.

**Quality targets for the first complete release:** all narration spans accounted for; 100% of requirements have a resolved status with explicit reason/evidence; every acquired file has verifiable provenance and digest; zero unapproved external file selections; zero fabricated timestamps; duplicate replays do not create duplicate media, charged calls, or source records. A coverage score is about verified usable assets, not counted search results.

## 6. Two-agent development plan and PR boundaries

**Governance:** agents work on separate, isolated feature branches/worktrees rooted in the latest accepted `main`. Both review PR #345/#346 and each other's open scope before every slice. They never edit/push/merge directly on `main`; do not reset, rebase, force push, or cherry-pick shared work. Each PR includes tests and exact head-SHA CI evidence. A PR does not become a dependency until it is approved and merged.

**Agent A: Source Intelligence / Coverage**
- Owns requirement interpretation beyond the exact-span PR #346 foundation, source-search job planning, semantic query expansion, provider discovery integration, grounded candidate verification, relevance ranking, gap/completeness evaluation.
- Primary new code area proposed: `src/katcha/editorial/source_intelligence/` with its own contracts and tests. Changes to legacy `assets.py`, `research.py` or `api/editorial_projects.py` require a coordinated integration slice so as not to conflict with #346.
- Explicitly does **not** implement generic downloading, object storage, video transcoding, or media deletion.

**Agent B: Rights-Safe Intake / Media Bin**
- Owns permitted acquisition capability checks, media validation, object lifecycle/dedupe, durable receipts, project-scoped library reference binding, export, preview availability, and media indexing preparation. May build shot index after end-to-end intake is stable.
- Primary new code area proposed: `src/katcha/editorial/media_intake/` and dedicated tests. Reuse existing storage/Clip Library/rights models. Any modification to existing rights authority or cross-channel membership requires explicit compatibility review.
- Explicitly does **not** rewrite script parsing, AI search prompts or generic provider discovery ranking.

**Coordinator (ChatGPT/operator):** approve/freeze contract version, assign independent slices, audit accepted `main`, open PRs and changed filenames before each launch, check tests/CI and rights/security concerns, request changes, merge **only when explicitly authorized**, and give next assignments after accepted merges. Two concurrently running Codex sessions do not themselves implement a perpetual manager. Do not claim that I can start background Codex tasks from this conversation without a supported action/scheduler.

### Work order — deliver a meaningful vertical slice early

| Gate / PR | Owner | Depends on | Deliverable | Required proof |
|---|---|---|---|---|
| **G0. Baseline + contract freeze** | Coordinator + A/B review | `main`, open PR inspection | Shared field types, provider permission states, feature flag, non-overlap map; decide PR #346 integration path | No duplicate schemas; CI baseline recorded; rights/legacy risk documented |
| **A1. Comprehensive requirement coverage** | A | PR #346 accepted or deliberate independent contract adapter | Enrich exact script inventory with scene/entity/shot/search requirements, explicit ambiguity, >30-item pagination/batches, requirement coverage and gaps | Exact original text preserved; 100% beat coverage; fixture has 100+ cues; stable IDs and partial invalidation |
| **B1. Project Media Bin core** | B | G0, existing Clip Library | Read-only then additive project refs for owned clips, remote URL references, images and source lineage; API without changing discovery | Channel isolation, legacy clip compatibility, idempotent linking, no byte copies, durable status |
| **A2. AI scout task planner** | A | A1 | SourceSearchJobV1, bounded specialist job dispatch, real grounded links and provider-specific queries, replay/quotas/gap receipts | Relevant fixture queries; provider unavailable/offline/rate-limit; fake provider responses cannot invent source links |
| **B2. Acquisition capability and receipt engine** | B | B1, G0 | Source-type permission gates, resumable allowed media fetch, hash/ffprobe/image validation, state/rights evidence, safe object storage | Unauthorized YouTube URL remains reference-only; SSRF/redirect/mismatch/oversize blocked; crash/dedupe/revocation replay |
| **A3. Candidate relevance + substitution** | A | A2 | Ranker based on observed metadata, verified link authority, optional model relevance and rights/cost features; source alternatives and citation evidence | Deterministic fixture ranking; no ungrounded URL; no cross-provider rights bypass |
| **B3. Multi-type assets + project export** | B | B2 | Supported still/video/audio media, provenance, thumbnails (only permitted), working export with physical files and `references/links.csv` | ZIP/package roundtrip, image/video dedup, metadata preserved, export no secrets/expired signed URLs |
| **I1. End-to-end search→bin integration** | A (B reviews) | A3, B3 accepted | API/Temporal workflow joins requirement planner, scouts, verifier, collector and media bin. Small Studio "Find & prepare media" flow. | Fixture script produces real bin items, saved references and explicit coverage; restart does not duplicate operations |
| **B4. Shot-level searchable index** | B | I1, validated media | Measured shot/frame evidence and tagged/timecoded searchable segments; no claim of exhaustive inspection with sparse frames | Known-shot ground-truth seek and tolerance, content-change invalidation, bounded CPU and disk |
| **A4. Requirement-to-shot selector** | A | B4 | Evidence-backed shot recommendations, gaps, alternatives, manual lock and source links | Chosen in/out within measured media bounds; rights revalidated; no hallucinated sequence |
| **I2. Editorial editing handoff** | B (A reviews) | A4 | Versioned bindings into existing Direction/Storyboard pipeline; no full renderer redesign here | Existing editorial source-selection/render compatibility; multi-shot remains separate #345 dependency |
| **L1. Live authorized acceptance** | Coordinator + both | I1, B4, A4, I2 | Real project rehearsal with operator-authorized assets and production budget | Actual URLs/permissions, checksums, timestamps, bin and export evidence; honest blockers/limitations |

**Parallelism discipline:** A1 and B1 can start at the same time *after G0*, because their primary files and responsibilities are disjoint. A2/A3 and B2/B3 can overlap after shared contracts are frozen. One agent owns the integration PR and the other reviews it. If PR #346 is still draft, Agent A should finish/evaluate that work rather than duplicate it; Agent B can implement B1 safely from accepted `main`.

## 7. First acceptance workload: VisionQuest media package

**Inputs:** operator-supplied VisionQuest commentary script (approximately 13–14 minutes as previously planned), available trailer, optional narration WAV, actual configured/authorized provider access, budget and usage scope. Do not assume the script or trailer grants third-party content rights.

**Test A — deterministic CI (mandatory before any live run):** seeded script with 100+ visual requirements; owned footage with known shot times; cleared stills; an official reference-only YouTube hit; a direct licensed downloadable video and image from simulated APIs; duplicate URLs; an expired license; a 403 or unavailable media source; malicious redirect/private IP; an interrupted download; and a provider outcome of unknown billing. Results must be deterministic, no real external spending, with saved receipts and resumable state.

**Test B — authorized real search (explicit operator authorization):** process genuine script, search enabled real providers, produce a media package showing downloadable acquired files and source-only reference entries. Validate provider receipts, relevance and editor usability by spot-checking each prioritized passage. Missing rights/media appear as blockers with alternatives; do not fabricate successful acquisition. Run with strict spend/traffic caps.

**Test C — production handoff:** for all production-eligible acquired clips, return known-good source-relative intervals and candidate reasons. User can inspect/export files and links and manually replace a source. Later first-cut integration must produce a private review render under the broader PR #345 acceptance requirements, not hide unfulfilled requirements behind filler.

**Required acceptance report** (persist beside the project): project/revision SHA; script span/beat count; requirements total and priorities; grounded references count; eligible acquired assets (by type/bytes); rights and denied/expired counts; uniqueness/near-duplicates; shot-index duration/coverage; source citations and timestamps; `fulfilled/reference_only/blocked/unresolved` by requirement; provider operations/usage/cost/unknown-charge receipts; crash/retry evidence; storage/egress estimate; manual overrides; export manifest and checksums. **Never equate link count with usable asset coverage.**

## 8. User-facing Media Bin and controls

The first screen is task-focused, not technical: `Find & prepare media` → progress → sortable source grid. Each row displays a plain-language state: **Ready to use**, **Ready for review**, **Link saved**, **Needs permission**, **Could not obtain**, **Searching alternatives**. Filters: beat/scene, video/still/graphic/audio, publisher, tag, rights, downloaded vs link-only, best match, review-needed. Detail drawer shows why matched, the exact source, available time ranges, license/credit, last verification, bytes/hash, and worker receipt. Provide **Replace**, **Lock selection**, **Add my own file**, **Retry permitted source**, **Search alternatives**, **Export assets and links**. Existing `docs/UX_GUIDELINES.md` and Aerith shell remain authoritative.

Do not require the operator to approve routine searches. Ask only where authorization/ownership, paid provider use, ambiguous facts, rights, or publication decisions require it. Preserve manually pinned clips and edits through replanning/retries.

## 9. Engineering test and release gates

For every PR, record exact base/head SHA, scope/changed files, targeted tests, CI checks, runtime fixture receipts and limitations. Existing project commands include `pytest -q`, `ruff check src tests`, `mypy src`, `node renderer/test/editorial.mjs` (when touching renderer) and browser tests as applicable; use actual workflow names from `.github/workflows` rather than assuming a guessed status-check name. Tests should cover:

- **Contracts:** 120k-character script boundary, exact Unicode and source-span preservation, >30 requirements and incremental edits, schema roundtrip, historical migration compatibility.
- **Discovery:** web grounding, provider cursor/retry/backoff, no forged direct files, domain/canonical URL normalization, quota/cost ceiling, misclassified publisher/media, near-duplicate sources and relevance scoring.
- **Safety/rights:** stale license, rights revocation, reference-only YouTube/press links, operator authorization, channel isolation, expired URLs, SSRF rebinding/redirect, huge file/decompression limit, MIME mismatch, invalid transcoding and malicious metadata.
- **Recovery:** process killed after a partial download; re-entry after successful provider call; repeated request identity; digest mismatch; provider timeout with unknown charge; duplicate callbacks; migration failure rollback with no data loss.
- **Quality:** measured video duration, observed shot citation tolerance, audio/video source clarity, correct metadata and physical export; rejected and unresolved items remain discoverable in the Media Bin.
- **UX:** task labels, status/empty/error states, keyboard/mobile layout, preserving edited selections on retry, rights visibility, package export success and accurate links.

**Do not claim production-ready** without a live authorized provider test. Do not claim accepted `main` capability from an open/draft PR or a deterministic fake provider. If CI is blocked by unrelated issues, report exact failed checks and do not auto-merge or silently waive them.

## 10. Agent kickoff prompts (after G0)

### Agent A — Source Intelligence

> You are Agent A, Katcha Source Intelligence owner in `Deveadra/katcha`. Your mission is to create a genuinely useful, testable script-to-source research pipeline. Read `AGENTS.md`, `docs/UX_GUIDELINES.md`, this plan, the broader draft PR #345, and draft PR #346 before editing; inspect accepted `main` and open PRs to avoid duplicate work. Begin with A1 (or finish/reconcile PR #346 first if it is still pending), then progress through scoped A2/A3 pull requests only after dependencies are accepted. Own requirement coverage, semantic search hypotheses, bounded provider scout dispatch, grounded candidate verification, relevance ranking and gaps. Never implement uncontrolled crawling, arbitrary downloads, rights clearance, production deployment or a broad UI rewrite. Preserve imported script bytes, existing provider receipts and historic Editorial contracts. Execute relevant tests and PR CI; report a runnable proof, exact files/commits/tests, remaining gaps, and your next proposed slice. Branch + PR only, never main push/auto-merge.

### Agent B — Rights-Safe Acquisition & Library

> You are Agent B, Katcha Media Intake owner in `Deveadra/katcha`. Read `AGENTS.md`, `docs/UX_GUIDELINES.md`, this plan, draft PR #345/#346, current `main`, rights and existing Clip Library/storage models. Begin with B1: a durable, channel-safe project-scoped Media Bin that references canonical existing clips and external URLs without duplicating bytes. Then implement B2/B3 in independent, verified PRs: permission-gated file acquisition, idempotent receipts, validated images/videos, hashing and dedup, provenance, physical project export with links. You own this area, not script analysis or semantic search. A found public YouTube URL is reference-only absent explicit permission: do not rely on yt-dlp to bypass platform terms. Test SSRF, redirects, file checks, rights expiry, unknown outcomes, restart/dedupe, project isolation and legacy compatibility. Never weaken existing policies, alter production secrets, deploy, merge, or push main. Report exact evidence, blocked capabilities, and handoff contracts for Agent A.

### Coordinator review checklist

- Verify current accepted main SHA and open PRs **before** giving either agent an assignment.
- Confirm file ownership and shared contract version; prevent simultaneous edits to the same legacy API/model/migration file.
- Require each PR to state *implemented, tested, live-verified, not-yet-proven* separately.
- Run security/rights/quality-focused review, not merely test-count review. Confirm all CI status checks on the **exact** PR head, then request explicit merge authorization.
- After acceptance, refresh both agents' baselines, update dependency status, and assign the next independent PRs. Keep a single evidence-based progress board with `accepted main`, `in review`, `blocked`, `not started` states.
- Raise architectural conflicts immediately and choose one integration owner; never let two agents resolve merge conflicts by discarding each other's work.

## 11. Milestones and stop/go decisions

- **M0 — Safe foundation:** accepted contract and compatibility test; existing draft PR #346 disposition known.
- **M1 — Intelligence works:** all supplied script spans mapped to requirements, real grounded provider references found via authorized search, query/cost receipts, explicit gap reasons. *Not equal to downloadable media.*
- **M2 — Intake works:** chosen permitted images and video are validated and stored; reference-only URLs preserved; Media Bin and reproducible export work; rights and crash recovery proven. **This is the first high-value release and should be prioritized over a redesigned editor.**
- **M3 — Footage becomes editable:** measured shot segments and search, exact source-relative ranges, beat bindings, no invented timecodes, safe handoff to existing Storyboard.
- **M4 — Automated video draft:** defer until M1–M3; integrate with multi-shot timelines, procedural graphics and first-cut renderer work tracked by PR #345.

**Definition of success for this initiative:** With just a script, owned starting media and authorized sources, Katcha automatically researches all requested visuals, collects all *permitted/available* assets, records every unresolved reference with meaningful next actions, produces a manual-inspectable/downloadable source package, and exposes verified clip segments and rights-aware bindings for AI editing. Human permissions and editorial judgment remain real gates; mechanical searching, downloading, tagging, indexing and organizing do not require continuous manual intervention.

---

### Technical/reference links

- Katcha base: https://github.com/Deveadra/katcha/tree/main
- Existing broader plan PR: https://github.com/Deveadra/katcha/pull/345
- Existing script inventory PR: https://github.com/Deveadra/katcha/pull/346
- YouTube API Services Developer Policies (2026): https://developers.google.com/youtube/terms/developer-policies
- Pexels API docs (illustrative possible authorized stock adapter; connection and compliance must be verified): https://www.pexels.com/api/documentation/
