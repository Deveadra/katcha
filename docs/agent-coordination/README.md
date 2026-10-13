# Katcha — autonomous Source Intelligence & Media Intake

Mission: Turn an entire script into grounded candidates, permitted media, an organized project Media Bin and evidence-backed editor handoff.

Read root AGENTS.md, COORDINATION_PROTOCOL.md, your role file and relevant backlog task. Refer to CONTRACTS.md and IMPLEMENTATION_PLAN.md as needed; do not repeatedly reread everything.

Agent A owns source intelligence and research. KSI-101 through KSI-105 are merged: exact-script requirements (#354), bounded search jobs (#349), grounded scouting (#353), query expansion (#358), and reference ranking/coverage (#360). Reuse these services and #346's exact script planner; do not implement another parser or scout. KIN-101 (#373) now connects these services to Editorial and the project Media Bin. KMI-107 (#375) and KSI-106 (#379) are accepted; the latter preserves measured evidence and operator choices.
Agent B owns media intake, project Media Bin persistence and authorized acquisition. KMI-101 through KMI-106 are accepted (#355/#359/#361/#364/#368/#369), covering durable Media Bin storage, scoped APIs, authorized fetching, integrity validation and portable export. Agent B reviewed KIN-101 and delivered accepted KMI-107 (#375); KIN-102 (#390) is accepted and binds approved measured shots into existing Storyboard/render contracts.

Self-service loop: writable unique feature branch; implement and test; open PR; green required checks and reviews, no conflicts; auto-merge; refresh main and begin next eligible task.
Either agent may edit required shared API/model/migration files when no active overlapping PR change exists. Check current migration heads. Coordinate real collisions, not theoretical ones.

Never push directly to main or force-push. Backlog is advisory; GitHub merged PRs and required CI prove progress. No unauthorized downloading, data destruction, paid provider calls or production deployment.
INSTALL.md is for initial setup only, not for an already installed pack.

## Accepted evidence snapshot — 2026-10-12

- KSI-103: [PR #353](https://github.com/Deveadra/katcha/pull/353), merge `6db0849c`, [CI](https://github.com/Deveadra/katcha/actions/runs/38168260044): 1,488 passed, 1 skipped; Python and PR Gate passed on PR head `b3ed978f`. Durable provider-reported candidates, reference-only handoffs, saved cursors and bounded gaps.
- KSI-101: [PR #354](https://github.com/Deveadra/katcha/pull/354), merge `85d43c8a`, [CI](https://github.com/Deveadra/katcha/actions/runs/38168430486): 1,475 passed, 1 skipped; Python and PR Gate passed on PR head `c2c73c1f`. Exact script preserved, exhaustive requirement adapter and selective dependency invalidation.

Each slice received independent review. Provider fixtures and metadata are not live source verification, download permission or production clearance. KIN-101 now composes these services through the scoped Editorial API and Assets panel; external coverage remains reference-only with zero usable media.

- KSI-104: [PR #358](https://github.com/Deveadra/katcha/pull/358), merge `ca741e65`, [CI](https://github.com/Deveadra/katcha/actions/runs/38169464285): 1,515 passed, 1 skipped; Python and PR Gate success at `7421fd3d`. Bounded semantic variants, complete follow-up batches and known-job replay.
- KSI-105: [PR #360](https://github.com/Deveadra/katcha/pull/360), merge `a5c38936`, [CI](https://github.com/Deveadra/katcha/actions/runs/38170990441): 1,558 passed, 1 skipped; Python and PR Gate success at `f1918633`. Authoritative-ledger ranking, provenance-preserving diversity and complete reference/gap reporting; usable media remains zero.
- KMI-101: [PR #355](https://github.com/Deveadra/katcha/pull/355), merge `02c1af92`, [CI](https://github.com/Deveadra/katcha/actions/runs/38169131991): 1,501 passed, 1 skipped; Python and PR Gate success at `c08da305`. Durable project references; no duplicate blobs or implicit clearance.
- KMI-102: [PR #359](https://github.com/Deveadra/katcha/pull/359), merge `e0feb19e`, [CI](https://github.com/Deveadra/katcha/actions/runs/38170806971): 1,535 passed, 1 skipped; Python, browser and PR Gate success at `da4ca57e`. Existing project API gains scoped Media Bin operations.

- KMI-103: [PR #361](https://github.com/Deveadra/katcha/pull/361), merge `6f04d041`, [CI](https://github.com/Deveadra/katcha/actions/runs/38171525896): Python Test step and PR Gate success at `3bd47694`. Permission/receipt contracts and durable outcomes; test count not captured, no live acquisition demonstrated.

- KMI-104: [PR #364](https://github.com/Deveadra/katcha/pull/364), merge `1e8971f6`, [CI](https://github.com/Deveadra/katcha/actions/runs/38191963615): Python and PR Gate passed on exact PR head `32cf8028`. Bounded, allowlisted authorized fetching, safe redirect/DNS checks, streamed size limits and durable no-retry treatment for unknown outcomes; no live media acquisition performed.
- KMI-105: [PR #368](https://github.com/Deveadra/katcha/pull/368), merge `79623048`, [CI](https://github.com/Deveadra/katcha/actions/runs/38193482371): Python and PR Gate passed on exact PR head `1f25c2a7`. PyAV-backed audio/video and image validation, SHA change invalidation and channel-scoped exact/perceptual duplicate evidence; synthetic fixtures only.
- KMI-106: [PR #369](https://github.com/Deveadra/katcha/pull/369), merge `e8116002`, [CI](https://github.com/Deveadra/katcha/actions/runs/38195177525): Python, Explorer and PR Gate passed on exact PR head `388997f5`. Deterministic portable ZIP, links CSV, sanitized references and rights/acquisition/validation evidence; includes bytes only for matching validated receipts with current production clearance. Fixture bytes only.

- Media Intake hardening: [PR #372](https://github.com/Deveadra/katcha/pull/372), merge `210ca432`, [CI](https://github.com/Deveadra/katcha/actions/runs/38212941963): 1,635 passed, 1 skipped; Python, Explorer and PR Gate passed on head `35067d34`. Pinned DNS transport, receipt concurrency/interrupted outcomes, current evidence and bounded ZIP responses; preserved in KIN-101 by normal merge.
- KIN-101: [PR #373](https://github.com/Deveadra/katcha/pull/373), merge `aebc0025`, [CI](https://github.com/Deveadra/katcha/actions/runs/38214738312): **1,652 passed, 1 skipped**; Python, Explorer, [launcher](https://github.com/Deveadra/katcha/actions/runs/38214738236) and PR Gate passed on final head `4aba15b8`. Seventeen script-to-Bin integration cases, scoped API, persisted requirements/coverage/provenance, canonical Bin/Clip reuse, visible gaps/rights, fenced bounded continuation and explicit unknown-outcome recovery. Independent backend/UI and Agent B Bin/export reviews passed. No live provider, paid acquisition or production proof.

- KMI-107: [PR #375](https://github.com/Deveadra/katcha/pull/375), merge `de563dcc`, [CI](https://github.com/Deveadra/katcha/actions/runs/38218457122): **1,680 passed, 1 skipped**; Python, Explorer and PR Gate passed on final head `34a313b4`. Additive durable index/receipts, measured source-relative visual-change intervals, sparse frame hashes/color tags, scoped retrieval, actual byte invalidation and current rights checks, bounded local decoder and fenced explicit recovery. Generated known-shot retrieval within 0.1s tolerance; no semantic scene recognition, OCR/speech/provider calls or production clearance. Consumer contract: `docs/MEASURED_MEDIA_INDEX.md`.

KIN-101's full handoff is documented in `docs/EDITORIAL_MEDIA_PREPARATION.md`.
KSI-106/#379 and KIN-102/#390 are accepted. KQA-101 remains planned full-path acceptance; KQA-102 has offline preparation only, with no live provider exercise.
Preserve the authoritative search ledger volume and existing operator annotations.

- KSI-106: [PR #379](https://github.com/Deveadra/katcha/pull/379), merge `94b332f519c90efd5b37aab36a0057918b4dffb3`, [CI](https://github.com/Deveadra/katcha/actions/runs/38272257656): **1,759 passed, 1 skipped** at head `aff9b6f31edd98c4c11f91cbb668c1782aa82566`; Python, Explorer and PR Gate succeeded. Durable measured proposals, scoped evidence and preserved locked choices; no semantic or live-rights claim.
- KIN-102: [PR #390](https://github.com/Deveadra/katcha/pull/390), merge `27b362c53cac17d1769c74ef13f5324601d9b547`, [CI](https://github.com/Deveadra/katcha/actions/runs/38281808012): **1,802 passed, 1 skipped** at head `30be860243b4fa3b579819fdaa1469dd3bad87a0`; all selected checks passed. Approved choices enter existing Storyboard/render with final rights rechecks. Independent review reproduced and verified the dispatch-claim race fix. `acceptance-media` and `full-startup` were skipped; separate local Editorial encode remains unverified.

KQA-101's complete deterministic source-package proof remains planned. The
[KQA-102 offline plan harness](../KQA102_OFFLINE_ACCEPTANCE.md) prepares scope,
source declarations, budgets and the evidence checklist only. No real provider
calls, source verification or production acceptance have occurred.

Offline KQA-102 preparation and search-ledger recovery are accepted in merged [PR #395](https://github.com/Deveadra/katcha/pull/395), merge `33bb80139dc84e707b3510a91c792b326d82d828`. Final head `3b6b6fdd4b0c9849b09b1850c4b8d3ab2353e011` passed [exact-head CI](https://github.com/Deveadra/katcha/actions/runs/38337429257): 1,909 passed, 1 skipped, including 31 real PostgreSQL fixture recovery cases and offline plan tests. Independent review addressed synchronous-WAL durability in both ledger transactions and restore quarantine. This accepts offline preparation, not the full KQA-101 path or live KQA-102 calls. Production cutover/activation remains a separately approved operator action.
