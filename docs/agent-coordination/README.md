# Katcha — autonomous Source Intelligence & Media Intake

Mission: Turn an entire script into grounded candidates, permitted media, an organized project Media Bin and evidence-backed editor handoff.

Read root AGENTS.md, COORDINATION_PROTOCOL.md, your role file and relevant backlog task. Refer to CONTRACTS.md and IMPLEMENTATION_PLAN.md as needed; do not repeatedly reread everything.

Agent A owns source intelligence and research. KSI-102 merged in #349, grounded scouting KSI-103 in #353, and exact-plan requirement integration KSI-101 in #354. Next KSI-104, then KSI-105 after its prerequisite is accepted. Reuse #346's merged script planner; do not implement another parser.
Agent B owns media intake, project Media Bin persistence and authorized acquisition. KMI-101 is in review in #355; verify its merge and required checks before KMI-102/KMI-103. No coordinator assignment needed.

Self-service loop: writable unique feature branch; implement and test; open PR; green required checks and reviews, no conflicts; auto-merge; refresh main and begin next eligible task.
Either agent may edit required shared API/model/migration files when no active overlapping PR change exists. Check current migration heads. Coordinate real collisions, not theoretical ones.

Never push directly to main or force-push. Backlog is advisory; GitHub merged PRs and required CI prove progress. No unauthorized downloading, data destruction, paid provider calls or production deployment.
INSTALL.md is for initial setup only, not for an already installed pack.

## Accepted evidence snapshot — 2026-10-11

- KSI-103: [PR #353](https://github.com/Deveadra/katcha/pull/353), merge `6db0849c`, [CI](https://github.com/Deveadra/katcha/actions/runs/38168260044): 1,488 passed, 1 skipped; Python and PR Gate passed on PR head `b3ed978f`. Durable provider-reported candidates, reference-only handoffs, saved cursors and bounded gaps.
- KSI-101: [PR #354](https://github.com/Deveadra/katcha/pull/354), merge `85d43c8a`, [CI](https://github.com/Deveadra/katcha/actions/runs/38168430486): 1,475 passed, 1 skipped; Python and PR Gate passed on PR head `c2c73c1f`. Exact script preserved, exhaustive requirement adapter and selective dependency invalidation.

Each slice received independent review. Provider fixtures and metadata are not live source verification, download permission or production clearance. Scouting remains a callable service; project API/Media Bin integration is later KIN-101 work.
