# Repository guidance

For user-facing Katcha changes, read and follow `docs/UX_GUIDELINES.md`. New UI must
use plain-language tasks, hide implementation details by default, explain empty
and error states, and preserve user input during recovery. Extend the existing
application and its authentication/launcher integration.

## Coordinated Source Intelligence & Media Intake development

For work on script-directed source discovery, rights-safe acquisition, project Media Bin,
scene indexing, or Editorial asset handoff, read the tracked coordination pack at
`docs/agent-coordination/README.md` **before editing**. Its task list is planning state,
not proof of implementation. Read `docs/agent-coordination/IMPLEMENTATION_PLAN.md`,
`CONTRACTS.md`, and the assigned Agent A/B instructions. Project scope must not
weaken this repository's existing UI, safety, media, or production controls.

**Guardian Protocol:** use an isolated feature branch/worktree and a reviewed PR; never
push to `main`, automatically merge, deploy, rewrite shared Git history, force-push,
or discard another agent's changes. Inspect accepted `main`, unmerged PRs and actual
file ownership first. Require tests and exact-head CI evidence; mark status accepted
only after a verified merge. No production credentials, speculative licensing,
unrestricted scraping, or unattended paid-provider calls. Stop for approval when
permission, cost, shared-file ownership, or compatibility boundaries are unclear.
