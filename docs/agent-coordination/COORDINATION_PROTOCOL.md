# Self-service parallel development

1. Use distinct feature branches and writable checkouts. Never share a working tree, push directly to main, force-push, rewrite shared history or overwrite other agents' work.
2. Pick dependency-ready tasks using live GitHub merged PRs and required CI, not stale AGENT_BACKLOG.json statuses. No standing coordinator authorization required.
3. The implementing agent owns necessary code, additive Alembic migrations and ordinary shared API/model edits in their own PR. Before editing shared behavior, inspect active PR diffs and migration heads. Coordinate ONLY concrete collisions. Two parallel additive migrations may require a normal merge migration or equivalent tested reconciliation.
4. Implement, test and open a focused PR. Auto-merge only when required GitHub checks/reviews pass and GitHub reports mergeable without unresolved conflicts. Refresh main and continue.
5. If the Codex worktree or shared Git metadata is outside writable roots, move to an authorized writable checkout or reconnect the workspace; instructions cannot override permissions. Report exact path/error rather than bypassing access controls.
6. Preserve Editorial behavior, channel isolation and user data. No destructive migrations, unauthorized media downloads, unapproved provider costs, production changes or credential changes.

Report briefly: task ID, functionality, tests/CI, PR and next task. Accept only with merged PR + passing required checks. Backlog is a snapshot, not a lock; update without racing concurrent agents.
