# Agent B — Rights-safe Intake / Project Media Bin

**Mission:** Convert authorized source candidates and existing owned media into
verified, channel-isolated project references and permitted files with durable
provenance. Link-only media remains useful. Prioritize a functional Media Bin,
then a lawful acquisition/validation engine and portable export.

## Required read sequence

Root `AGENTS.md` → `docs/UX_GUIDELINES.md` → pack `README.md` → full
`IMPLEMENTATION_PLAN.md` → `CONTRACTS.md` → `COORDINATION_PROTOCOL.md` →
`AGENT_BACKLOG.json` → this file. Inspect accepted `main`, current Clip Library,
`ObjectStore`, Editorial source uploads, rights rules, migrations and open PRs.

## Your areas and exclusions

**Own:** `src/katcha/editorial/media_intake/`, tests for channel/project
binding, permitted previews, ownership/rights evidence, safe acquisitions,
media probes, digest/dedupe, receipts, recovery, and export.
**Not yours:** script parser, AI prompts, semantic web research, candidate ranker,
Agent A's `source_intelligence/`, or reimplementation of shared Clip Library.

Use existing managed object storage; no duplicate blob storage solely because a
project references a clip. A found public YouTube URL is reference-only absent
permission. Neither a downloader library nor an HTTP 200 grants rights.

## Current B handoff

KMI-101 through KMI-107 are accepted with merged PRs and exact-head CI evidence.
Reuse the durable Media Bin, scoped permission/receipt contracts, bounded
acquisition, integrity validation and portable export; fixtures do not demonstrate
live source acquisition or confer rights.

**`KMI-107`: Measured shot/frame media index and search** is accepted in #375,
merge `de563dcc`, with exact-head CI (1,680 passed, 1 skipped). Reuse its durable
`SceneIndexV1`, source SHA/timebase, sparse keyframe descriptors, scoped services,
current permission checks and bounded decoder receipts. See
`../MEASURED_MEDIA_INDEX.md` for the initial visual-token search and coverage limits.
No semantic actions, OCR, speech, provider calls or production rights are inferred.

**KIN-102/#390 is accepted**, merge `27b362c53cac17d1769c74ef13f5324601d9b547`,
with exact-head CI at `30be860243b4fa3b579819fdaa1469dd3bad87a0`:
1,802 passed, 1 skipped; Python, Explorer, Renderer, ranked-render E2E and PR Gate
succeeded in [CI 38281808012](https://github.com/Deveadra/katcha/actions/runs/38281808012).
Launcher and the selected runtime-image build also succeeded. Editorial now binds
explicit measured choices into saved Storyboard beats, preserves other visuals
and history, and checks rights/choices after dispatch claim before rendering and
again before promotion. See `../APPROVED_SHOT_STORYBOARD_HANDOFF.md`.
KMI-107 operator indexing/search integration is also merged in #389. Fixtures
and mocked dispatch races establish offline behavior; `acceptance-media` and
`full-startup` were skipped, and the separate local Editorial encode is unverified.

**Next eligible acceptance work: KQA-101** remains planned full-path acceptance.
KQA-102 has an offline scoped plan/checklist harness only; it cannot execute live
calls or certify rights. See `../KQA102_OFFLINE_ACCEPTANCE.md`. Separate operator
source/provider authorization and explicit traffic/spend caps remain the real
live gate; no coordinator ownership approval is introduced.

## Follow-on ordered tasks

`KMI-102` Media Bin API → `KMI-103` acquisition capability/receipt gates →
`KMI-104` safe authorized fetching → `KMI-105` validation/deduplication →
`KMI-106` multi-type export → integration `KIN-101` (A owner, B reviewer).
After KIN-101, `KMI-107` indexes verified shots; later `KIN-102` (B owner)
provides editor handoff. Use backlog dependencies as intent; live merged PRs and required CI prove eligibility.

## Minimum delivered evidence

- Existing Clip Library functionality preserved; every bin item has real
  source or original reference and recorded permission state.
- Reproducible tests for cross-channel denial, idempotency, metadata integrity,
  rights revocation, SSRF, redirects and interrupted fetches when applicable.
- Unknown rights do not promote items to use in a render; denied/unavailable
  references remain visible and actionable.
- Export includes only permitted physical media plus useful durable links;
  no secrets or transient signed URLs.
- Branch, SHA, PR, changed paths, exact test commands/CI, risks, next handoff.

Auto-merge PRs after required checks/reviews pass and continue. Never push directly to main, force-push, rewrite history or deploy.

Offline KQA-102 preparation and search-ledger recovery are reviewed in [PR #395](https://github.com/Deveadra/katcha/pull/395). Head `594996b2525179325b671c05023afe6db11a558f` passed [exact-head CI](https://github.com/Deveadra/katcha/actions/runs/38336337419): 1,886 passed, 1 skipped, including real PostgreSQL fixture recovery and offline plan tests. Final head checks are required before merge. This evidence does not accept the full KQA-101 path or authorize live KQA-102 calls.
