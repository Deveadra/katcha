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

## Initial GitHub situation (historical, verify again)

- At pack creation the latest accepted main was `3d08e5862bedb972c54e4a6ea6e3566354ec9881`.
- Draft PR #345 contains a broader autonomous first-cut plan.
- Draft PR #346 adds an initial deterministic script/visual plan. It **is not** accepted until verified merged.
- These are snapshots only. Check current main, PRs and CI before every task.
- Source-specific implementation must not duplicate in-flight PR #346.

## Starting two agents safely

**Gate `KCO-000` first:** owner/coordinator accepts the pack's contracts/ownership,
reconciles open PRs and ensures this pack is on accepted `main`. This pack itself
is not an implementation PR; merging it does not complete the source engine.

- **Agent A:** `KSI-102` (bounded research-job and provider capability foundation)
  may start independently while PR #346's script work remains open. `KSI-101`
  (requirements/coverage integration) requires a verified disposition of #346
  and an approved integration boundary. Do not fork a duplicate parser.
- **Agent B:** `KMI-101` (persistent project Media Bin records) can start as soon
  as `KCO-000` is accepted, independently of Agent A's research implementation.
- One ticket per PR unless the coordinator explicitly approves a small combined
  slice. Agent A owns `src/katcha/editorial/source_intelligence/`; Agent B owns
  `src/katcha/editorial/media_intake/`. Existing shared files require a declared
  integration owner before edits. Both may add disjoint tests.

For guarded local installation, see `INSTALL.md` and `install_pack.py`.
The installer stages files on a clean non-main branch, never pushes or merges.

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
- `AGENTS.md` — full **proposed** root `AGENTS.md` with additive guidance, preserving current contents.
- `ROOT_AGENTS_MD_ADDITION.md` — additive section for an existing root AGENTS file.
- `CONTRACTS.md`, `COORDINATION_PROTOCOL.md`, `AGENT_A.md`, `AGENT_B.md`.
- `AGENT_BACKLOG.json` — machine-readable tasks with ownership, dependencies, status evidence.
- `CHECKSUMS.sha256` — SHA-256 of all other pack artifacts.
- `INSTALL.md` / `install_pack.py` — safe, opt-in local staging on a feature branch.

**GitHub placement:** content files under `docs/agent-coordination/`; extend the
existing root `AGENTS.md` rather than installing a separate independent authority.
This bundle is planning/coordination documentation, not evidence that features ran.
