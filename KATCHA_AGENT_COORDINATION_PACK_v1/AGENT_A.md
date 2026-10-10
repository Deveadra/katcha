# Agent A — Source Intelligence / Coverage

**Mission:** Convert a finished, preserved script into an exhaustive, grounded,
rights-aware inventory of source candidates and explicit gaps. Build real working
code, not just a report. Highest-value end state is that Agent B can acquire
permitted assets from trustworthy, structured discovery outputs.

## Required read sequence

Root `AGENTS.md` → `docs/UX_GUIDELINES.md` → pack `README.md` → full
`IMPLEMENTATION_PLAN.md` → `CONTRACTS.md` → `COORDINATION_PROTOCOL.md` →
`AGENT_BACKLOG.json` → this file. Inspect accepted `main`, open PR #345/#346,
existing acquisition adapters, script planner, source/research services, tests.

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

## First assignment after KCO-000

**`KSI-102`: Bounded search-job and provider capability foundation.** This is
independent of still-draft PR #346. Provide typed, serializable `SourceSearchJobV1`
intent, deterministic planner fixture, quotas/replay/idempotence, provider
capability mapping and tests. Reuse existing discovery adapters. Build no
competing script parser and do not call real providers in CI.

If #346 is already merged/reconciled, the coordinator may assign `KSI-101`
first to integrate exact script spans and comprehensive requirements. Do not
independently decide that a draft is merged or cherry-pick its changes.

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
