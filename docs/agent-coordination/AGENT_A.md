# Agent A — Source Intelligence / Coverage

**Mission:** Convert a finished, preserved script into an exhaustive, grounded,
rights-aware inventory of source candidates and explicit gaps. Build real working
code, not just a report. Highest-value end state is that Agent B can acquire
permitted assets from trustworthy, structured discovery outputs.

## Required read sequence

Root `AGENTS.md` → `docs/UX_GUIDELINES.md` → pack `README.md` → full
`IMPLEMENTATION_PLAN.md` → `CONTRACTS.md` → `COORDINATION_PROTOCOL.md` →
`AGENT_BACKLOG.json` → this file. Inspect accepted `main`, current PRs (including draft #349),
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

## Current assignment

**`KSI-102`: Bounded search-job and provider capability foundation.** Draft
PR #349 already implements this workstream; continue/review that PR rather
than creating a duplicate. Provide typed `SourceSearchJobV1` intent, bounded
replay/idempotency, provider capabilities and deterministic tests. No real
provider calls in CI. PR #346's script planner is merged; `KSI-101` is a
separate assignment requiring approved shared-file ownership and reuse of
that planner, never a competing parser.

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
