# Repository guidance

For user-facing Katcha changes, read and follow `docs/UX_GUIDELINES.md`. New UI must
use plain-language tasks, hide implementation details by default, explain empty
and error states, and preserve user input during recovery. Extend the existing
application and its authentication/launcher integration.

## Autonomous parallel development
Use docs/agent-coordination for role, contracts and backlog. Live merged PRs and CI outrank stale backlog state.
Each agent owns its own feature branch and necessary implementation, including ordinary API/model changes and additive Alembic migrations. No coordinator-appointed owner or routine permission request. Check active PRs and migration heads to avoid real collisions.
Never push directly to main, force-push, rewrite history or discard another agent's work. Create focused PRs; auto-merge when required checks/reviews pass and no conflicts remain, then continue to the next eligible task. Never auto-merge failing or unverified code.
Use separate checkouts/worktrees WITHIN authorized Codex writable workspace. If a worktree or Git metadata is unwritable, reconnect or switch to a permitted checkout; do not bypass sandbox restrictions.
Preserve channel isolation, Editorial compatibility and existing data. Stop for genuine code conflicts, destructive migrations, unapproved provider spend/production actions or media rights concerns—not routine file ownership.
