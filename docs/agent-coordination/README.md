# Katcha — autonomous Source Intelligence & Media Intake

Mission: Turn an entire script into grounded candidates, permitted media, an organized project Media Bin and evidence-backed editor handoff.

Read root AGENTS.md, COORDINATION_PROTOCOL.md, your role file and relevant backlog task. Refer to CONTRACTS.md and IMPLEMENTATION_PLAN.md as needed; do not repeatedly reread everything.

Agent A owns source intelligence and research. KSI-102 merged in PR #349. Next KSI-103. Reuse PR #346's merged script planner.
Agent B owns media intake, project Media Bin persistence and authorized acquisition. Start KMI-101, including the additive model, Alembic migration and tests. No coordinator assignment needed.

Self-service loop: writable unique feature branch; implement and test; open PR; green required checks and reviews, no conflicts; auto-merge; refresh main and begin next eligible task.
Either agent may edit required shared API/model/migration files when no active overlapping PR change exists. Check current migration heads. Coordinate real collisions, not theoretical ones.

Never push directly to main or force-push. Backlog is advisory; GitHub merged PRs and required CI prove progress. No unauthorized downloading, data destruction, paid provider calls or production deployment.
INSTALL.md is for initial setup only, not for an already installed pack.
