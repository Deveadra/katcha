# Agent A — Source Intelligence / Coverage

**Mission:** Convert a finished, preserved script into an exhaustive, grounded,
rights-aware inventory of source candidates and explicit gaps. Build real working
code, not just a report. Highest-value end state is that Agent B can acquire
permitted assets from trustworthy, structured discovery outputs.

## Required read sequence

Root `AGENTS.md` → `docs/UX_GUIDELINES.md` → pack `README.md` → full
`IMPLEMENTATION_PLAN.md` → `CONTRACTS.md` → `COORDINATION_PROTOCOL.md` →
`AGENT_BACKLOG.json` → this file. Inspect accepted `main`, current PRs, the merged #349 search-job foundation,
the merged #346 script planner, existing adapters, source/research services, and tests.

## Your areas and exclusions

**Own:** `src/katcha/editorial/source_intelligence/`, dedicated tests, versioned
requirements/search-job/candidate adapters, query expansion, grounded provider
search, metadata verification and ranking, repeatable coverage reports.
**Not yours:** download network transport, ObjectStore internals, retention,
rights grants, `media_intake/`, deployment, universal legacy API rewrites or
automatic publisher access. External sites are *leads*, not downloads.

Do not rewrite locked narration. Handle >30 visual requirements and unknown
scenes without fabricating timestamps. Verify source/publisher and retain
actual search receipts; track provider budgets, cursors, rate limits, and
nontransient failures. AI may propose, never approve a license.

## Current A handoff

KSI-101 through KSI-105 and **KIN-101/#373** are accepted. Reuse the exact-plan
adapter, bounded search ledger, grounded candidates, reference ranking and the
scoped Editorial preparation API/Assets panel. KIN-101 merged at `aebc0025` after
final-head CI (1,652 passed, 1 skipped), independent backend/UI review and Agent B
Bin/export review. See `../EDITORIAL_MEDIA_PREPARATION.md` for the executable
handoff, persistent evidence volume and explicit recovery contract.

**Next A task: `KSI-106`**, after Agent B's accepted **KMI-107** measured shot index.
KMI-107 is now dependency-ready. Do not duplicate its index or invent scene/shot
intervals while waiting. Reference metadata and fixture receipts do not prove
usable media, rights clearance, observed scenes or source-relative times.

## Follow-on ordered tasks

`KSI-101`/`KSI-103` → `KSI-104` → `KSI-105` → integration `KIN-101` (A owner).
Later, after B shot index, `KSI-106` selects evidence-backed shot intervals.
Use the backlog dependency graph as intent; live merged PRs and passing CI prove eligibility.

## Minimum delivered evidence

- Executable functionality, versioned bounded payloads, deterministic tests,
  provider error/retention and repeated-request cases.
- No forged grounded URLs, fabricated scene observations, silent coverage loss,
  unlimited crawling or unauthorized paid spend.
- A candidate handoff example that B can attach to a project bin; distinguish
  fixture/test from real authorized discovery.
- Branch, SHA, PR, changed paths, exact test commands/CI, risks, next handoff.

Coordinate only real shared-file conflicts. Auto-merge a PR after required checks/reviews pass and continue. No direct main pushes, force-push or deployment.
