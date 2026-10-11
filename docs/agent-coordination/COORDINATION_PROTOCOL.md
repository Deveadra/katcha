# Coordination, Guardian Protocol and PR acceptance

## Non-negotiable Git and production rules

1. Inspect `main`, current Git tree, open PRs, changed files, existing migrations,
   CI runs and your assigned ticket before starting. `AGENT_BACKLOG.json`
   tracks **intent**, never supersedes GitHub or runtime evidence.
2. Use an isolated feature branch/worktree from the latest accepted `main`; no
   two agents may own the same worktree or change the same shared files in
   parallel. No unreviewed pushes to `main`, automatic merges, production
   deployments, reset, rebase, cherry-pick, force push, or destructive cleanup.
3. No changes to production rights policy, storage membership rules, expensive
   providers, deployment, or credentials without explicit authorization and
   regression proof. Preserve historical Editorial APIs, schemas and rendering.
4. Agent A owns `src/katcha/editorial/source_intelligence/` and associated tests;
   Agent B owns `src/katcha/editorial/media_intake/` and associated tests.
   Shared API files (`src/katcha/api/editorial_projects.py`, others), migration
   heads, `AGENTS.md`, editorial models and web UI are **reserved** for one
   coordinator-appointed integration owner per PR. PR #346 is merged; inspect its
   accepted shared-file changes before proposing integration edits.
5. If work overlaps, **stop that slice**, report exact paths and handoff request.
   Continue only independent, safe work. Do not resolve overlap by resetting
   someone else's work or overwriting their code.

## Task selection

- `KCO-000` is accepted (PR #347 with passing CI; pack moved in #348/#350).
  Coordinator checks the latest `main`, active PRs, migrations and changed
  files before assigning nonoverlapping tasks.
- Each agent takes **one task per PR** unless explicitly authorized otherwise.
- A task is eligible only if its `depends_on` tasks are accepted on current
  `main` **and** any `external_gates` are explicitly satisfied.
- `KSI-102` merged via PR #349; A may take `KSI-103` after coordinator
  assignment. B starts `KMI-101` independently. `KSI-101` reuses the merged
  PR #346 planner and needs an assigned shared-file integration owner.
- After a merge, re-read `main`, recheck migration heads and reassign from the
  actual updated DAG. Do not manufacture the next phase's completion.

## PR scope and validation

Each PR body must contain:

- Task ID, objective, base SHA, head SHA, files changed and boundary checks.
- Contracts/migration impact; explicit note on existing PR collisions.
- Completed changes; tests run with exact command, outcome and environment.
- CI workflow/check URLs and statuses on the **exact head SHA**. Never treat a
  queued/inconclusive/failed check as passing.
- Security/rights review: permission gates, URL/SSRF, isolation, billing and
  compatibility, with not-applicable explanations when truly irrelevant.
- Fixture vs authorized live evidence, limitations, unresolved risks, rollback
  considerations, and next dependency.

Use appropriate verified repo checks: Python `pytest -q`, `ruff check src tests`,
`mypy src`; renderer and browser tests when paths warrant them. Inspect actual
`.github/workflows/*.yml` for the required CI check names. Check migrations and
existing behavior before calling a change regression-safe. A fixture is not
production or live provider verification.

## Review and status lifecycle

`planned` → `in_progress` → `in_review` → `accepted`; alternatively `blocked` or
`superseded` with evidence. **Only a verified merge to `main` plus required CI
allows `accepted`.** Status and evidence updates to `AGENT_BACKLOG.json` are
coordinator-owned (dedicated reviewed changes, or together with an unrelated
PR only when the coordinator explicitly grants file ownership). Agent PRs
must not race to edit this JSON. An agent's self-report cannot mark acceptance.

Backlog entries specify `status_evidence: null` until linked proof exists.
Evidence should include merged PR URL, accepted SHA, exact checks, test outputs,
verification level (`implemented | verified_offline | verified_live`), and date.
Mark blocked with reproducible cause and smallest safe next action; never claim
blocked = complete or open PR = accepted. Reconcile weekly or after merges.

## Provider work and recovery

LLM workers generate suggestions; typed deterministic activities enforce network,
permitted content acquisition, budget, rights and idempotency. No bot can invent
clearance. Do not implement arbitrary paid retries after outcome `unknown`.
Prevent SSRF/redirect/private network fetches and obey provider terms; record
reference-only sources rather than circumvent downloads. Expired, revoked or
unknown rights block media promotion while preserving useful metadata.

## When to pause and ask the coordinator

Required: uncertain legal/rights authority; paid provider or infrastructure
spend; modifying common contracts owned by another PR; ambiguity about
preexisting media that could be destroyed; migration/data safety ambiguity;
CI unavailability masking required validation; prod network/storage changes;
or a requested operation contrary to Guardian Protocol.

## Report format

```text
Task / branch / PR / baseline SHA / head SHA
Done: implemented functionality (not aspirational)
Verified: exact tests + CI on head SHA, fixture/live separated
Not verified: missing acceptance, providers, deployment, permissions
Risks: rights, security, compatibility, cost, data, agent overlap
Next safest action: exact dependency and next eligible slice
```

## Human authority

The coordinator can review/assign/request fixes, but only the user may authorize
merging and live provider/production side effects. Parallel Codex tasks do not
by themselves implement unattended orchestration or GitHub backlog syncing.
