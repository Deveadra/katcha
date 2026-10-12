# Katcha — autonomous Source Intelligence & Media Intake

Mission: Turn an entire script into grounded candidates, permitted media, an organized project Media Bin and evidence-backed editor handoff.

Read root AGENTS.md, COORDINATION_PROTOCOL.md, your role file and relevant backlog task. Refer to CONTRACTS.md and IMPLEMENTATION_PLAN.md as needed; do not repeatedly reread everything.

Agent A owns source intelligence and research. KSI-101 through KSI-105 are merged: exact-script requirements (#354), bounded search jobs (#349), grounded scouting (#353), query expansion (#358), and reference ranking/coverage (#360). Reuse these services and #346's exact script planner; do not implement another parser or scout. Next A integration KIN-101 remains gated on KMI-106.
Agent B owns media intake, project Media Bin persistence and authorized acquisition. Core KMI-101 (#355), API KMI-102 (#359) and permission/receipt KMI-103 (#361) are accepted; KMI-104 is the next dependency-ready B task. Permitted acquisition, file validation and export remain later work.

Self-service loop: writable unique feature branch; implement and test; open PR; green required checks and reviews, no conflicts; auto-merge; refresh main and begin next eligible task.
Either agent may edit required shared API/model/migration files when no active overlapping PR change exists. Check current migration heads. Coordinate real collisions, not theoretical ones.

Never push directly to main or force-push. Backlog is advisory; GitHub merged PRs and required CI prove progress. No unauthorized downloading, data destruction, paid provider calls or production deployment.
INSTALL.md is for initial setup only, not for an already installed pack.

## Accepted evidence snapshot — 2026-10-11

- KSI-103: [PR #353](https://github.com/Deveadra/katcha/pull/353), merge `6db0849c`, [CI](https://github.com/Deveadra/katcha/actions/runs/38168260044): 1,488 passed, 1 skipped; Python and PR Gate passed on PR head `b3ed978f`. Durable provider-reported candidates, reference-only handoffs, saved cursors and bounded gaps.
- KSI-101: [PR #354](https://github.com/Deveadra/katcha/pull/354), merge `85d43c8a`, [CI](https://github.com/Deveadra/katcha/actions/runs/38168430486): 1,475 passed, 1 skipped; Python and PR Gate passed on PR head `c2c73c1f`. Exact script preserved, exhaustive requirement adapter and selective dependency invalidation.

Each slice received independent review. Provider fixtures and metadata are not live source verification, download permission or production clearance. Scouting remains a callable service; project API/Media Bin integration is later KIN-101 work.

- KSI-104: [PR #358](https://github.com/Deveadra/katcha/pull/358), merge `ca741e65`, [CI](https://github.com/Deveadra/katcha/actions/runs/38169464285): 1,515 passed, 1 skipped; Python and PR Gate success at `7421fd3d`. Bounded semantic variants, complete follow-up batches and known-job replay.
- KSI-105: [PR #360](https://github.com/Deveadra/katcha/pull/360), merge `a5c38936`, [CI](https://github.com/Deveadra/katcha/actions/runs/38170990441): 1,558 passed, 1 skipped; Python and PR Gate success at `f1918633`. Authoritative-ledger ranking, provenance-preserving diversity and complete reference/gap reporting; usable media remains zero.
- KMI-101: [PR #355](https://github.com/Deveadra/katcha/pull/355), merge `02c1af92`, [CI](https://github.com/Deveadra/katcha/actions/runs/38169131991): 1,501 passed, 1 skipped; Python and PR Gate success at `c08da305`. Durable project references; no duplicate blobs or implicit clearance.
- KMI-102: [PR #359](https://github.com/Deveadra/katcha/pull/359), merge `e0feb19e`, [CI](https://github.com/Deveadra/katcha/actions/runs/38170806971): 1,535 passed, 1 skipped; Python, browser and PR Gate success at `da4ca57e`. Existing project API gains scoped Media Bin operations.

- KMI-103: [PR #361](https://github.com/Deveadra/katcha/pull/361), merge `6f04d041`, [CI](https://github.com/Deveadra/katcha/actions/runs/38171525896): Python Test step and PR Gate success at `3bd47694`. Permission/receipt contracts and durable outcomes; test count not captured, no live acquisition demonstrated.
