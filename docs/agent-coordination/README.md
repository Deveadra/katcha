# Katcha — Source Intelligence & Media Intake Coordination Pack

**Version:** 1.0, prepared 2026-10-10. **Repository:** `Deveadra/katcha`.

**North star:** From a completed script, Katcha autonomously builds an evidence-backed,
rights-aware, inspectable project Media Bin of permitted files and source references,
then hands verified timecoded assets to the existing editor. **M2 (usable Media Bin,
acquisition and export) is the first priority**, not unrelated UI or publishing features.

## What to read, in order

1. Repository root `AGENTS.md` and `docs/UX_GUIDELINES.md` (both remain authoritative).
2. `IMPLEMENTATION_PLAN.md` — original, complete 2026-10-10 source and intake plan.
3. `CONTRACTS.md` — shared proposed records, state invariants, handoff and ownership.
4. `COORDINATION_PROTOCOL.md` — branch/PR/review/recovery/ownership discipline.
5. `AGENT_BACKLOG.json` — dependency-aware work queue; statuses are not live by default.
6. Only your role: `AGENT_A.md` (research) or `AGENT_B.md` (intake).

## Verified integration baseline (2026-10-10; recheck before each task)

- Coordination pack merged in PR #347; CI passed on merge commit `0a0a50008e70f8cf37e054cebbc20a857e001054`.
- PR #345 (first-cut plan) and PR #346 (script visual planner) are merged; reuse their accepted code rather than building a second parser.
- Pack location is `docs/agent-coordination/` (PRs #348 and #350 relocated it). `AGENT_BACKLOG.json` is coordinator-maintained, not live GitHub state.
- Agent A's KSI-102 is tracked in draft PR #349 as of this snapshot. Continue that PR rather than restarting it.
- Always inspect current `main`, active PRs, CI and shared-file ownership; these facts can change.

## Starting two agents safely

**`KCO-000` is accepted:** the user approved the v1 coordination contracts and the
pack was merged with passing CI. This is approval to implement within the stated
ownership boundaries, **not** approval for unreviewed shared schema changes, merges,
paid providers or production deployments.

- **Agent A:** `KSI-102` (bounded research-job and provider capability foundation)
  continues in draft PR #349 until reviewed and merged. `KSI-101`
  (requirements/coverage integration) must reuse the merged PR #346 planner
  and obtain an approved shared-file integration boundary. Do not fork a duplicate parser.
- **Agent B:** `KMI-101` (persistent project Media Bin records) can start as soon
  as `KCO-000` is accepted, independently of Agent A's research implementation.
- One ticket per PR unless the coordinator explicitly approves a small combined
  slice. Agent A owns `src/katcha/editorial/source_intelligence/`; Agent B owns
  `src/katcha/editorial/media_intake/`. Existing shared files require a declared
  integration owner before edits. Both may add disjoint tests.

The pack is already tracked in this repository. `INSTALL.md` and `install_pack.py`
are for fresh installation only; **do not rerun the installer in this checkout**.

## Operational use

1. Select a task with owner and prerequisites satisfied on **accepted main**.
2. Create an isolated feature branch and record its base SHA, files, test plan.
3. Implement a functioning vertical slice and deterministic acceptance evidence.
4. Run relevant checks and open a focused PR, never to `main` directly.
5. An independent reviewer/coordinator validates code, rights, security, and
   passing required checks on the exact PR head. User authorizes merges.
6. After a verified merge, refresh task evidence/status through a **separate,
   coordinated change**; do not pretend this JSON is auto-synchronized.

## Evidence grades

`implemented` = code exists; `verified_offline` = relevant deterministic tests
passed; `verified_live` = authorized real provider end-to-end proof; `accepted` =
PR merged into current main and gates verified. None is interchangeable.

## Pack files

- `IMPLEMENTATION_PLAN.md` — unchanged source plan.
- `AGENTS.md` — reference snapshot; repository root `AGENTS.md` is authoritative and carries the additive coordination guidance.
- `ROOT_AGENTS_MD_ADDITION.md` — additive section for an existing root AGENTS file.
- `CONTRACTS.md`, `COORDINATION_PROTOCOL.md`, `AGENT_A.md`, `AGENT_B.md`.
- `AGENT_BACKLOG.json` — machine-readable tasks with ownership, dependencies, status evidence.
- `CHECKSUMS.sha256` — SHA-256 of all other pack artifacts.
- `INSTALL.md` / `install_pack.py` — safe, opt-in local staging on a feature branch.

**GitHub placement:** content files under `docs/agent-coordination/`; extend the
existing root `AGENTS.md` rather than installing a separate independent authority.
This bundle is planning/coordination documentation, not evidence that features ran.
