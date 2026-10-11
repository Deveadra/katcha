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

## Next eligible assignment

**`KSI-103`: Grounded multi-provider scouting and metadata verification.**
`KSI-102` merged via PR #349 with passing PR CI; do not redo it. Reuse its
bounded search-job/replay foundation. Implement grounded provider candidates,
metadata receipts, cursors and bounded failures, with deterministic fake-provider
tests and no unauthorized downloads or paid CI calls. Start after coordinator
assignment. `KSI-101` is a separate integration task that must reuse the merged
PR #346 script planner and obtain shared-file ownership approval.

## Follow-on ordered tasks

`KSI-101`/`KSI-103` → `KSI-104` → `KSI-105` → integration `KIN-101` (A owner).
Later, after B shot index, `KSI-106` selects evidence-backed shot intervals.
Dependencies in `AGENT_BACKLOG.json` are authoritative for eligibility.

## Minimum delivered evidence

- Executable functionality, versioned bounded payloads, deterministic tests,
  provider error/retention and repeated-request cases.
- No forged grounded URLs, fabricated scene observations, silent coverage loss,
  unlimited crawling or unauthorized paid spend.
- A candidate handoff example that B can attach to a project bin; distinguish
  fixture/test from real authorized discovery.
- Branch, SHA, PR, changed paths, exact test commands/CI, risks, next handoff.

Stop at any conflicting shared-file or permission boundary, not at useful
independent work. Never merge, deploy or push directly to `main`.
