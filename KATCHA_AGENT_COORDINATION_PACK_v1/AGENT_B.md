# Agent B — Rights-safe Intake / Project Media Bin

**Mission:** Convert authorized source candidates and existing owned media into
verified, channel-isolated project references and permitted files with durable
provenance. Link-only media remains useful. Prioritize a functional Media Bin,
then a lawful acquisition/validation engine and portable export.

## Required read sequence

Root `AGENTS.md` → `docs/UX_GUIDELINES.md` → pack `README.md` → full
`IMPLEMENTATION_PLAN.md` → `CONTRACTS.md` → `COORDINATION_PROTOCOL.md` →
`AGENT_BACKLOG.json` → this file. Inspect accepted `main`, current Clip Library,
`ObjectStore`, Editorial source uploads, rights rules, migrations and open PRs.

## Your areas and exclusions

**Own:** `src/katcha/editorial/media_intake/`, tests for channel/project
binding, permitted previews, ownership/rights evidence, safe acquisitions,
media probes, digest/dedupe, receipts, recovery, and export.
**Not yours:** script parser, AI prompts, semantic web research, candidate ranker,
Agent A's `source_intelligence/`, or reimplementation of shared Clip Library.

Use existing managed object storage; no duplicate blob storage solely because a
project references a clip. A found public YouTube URL is reference-only absent
permission. Neither a downloader library nor an HTTP 200 grants rights.

## First assignment after KCO-000

**`KMI-101`: Project Media Bin durable core.** Implement additive bin records
which safely reference existing channel-owned Clip Library media and saved
external links without touching Agent A's parser, live providers, or downloader.
Support stable IDs, project/revision binding, provenance, channel isolation,
idempotent writes and status reads. Include DB migration compatibility tests.
Avoid editing files currently owned by PR #346; request an integration owner
for shared API/model/migration heads.

## Follow-on ordered tasks

`KMI-102` Media Bin API → `KMI-103` acquisition capability/receipt gates →
`KMI-104` safe authorized fetching → `KMI-105` validation/deduplication →
`KMI-106` multi-type export → integration `KIN-101` (A owner, B reviewer).
After integration, `KMI-107` indexes verified shots; later `KIN-102` (B owner)
provides editor handoff. Dependencies in backlog decide actual eligibility.

## Minimum delivered evidence

- Existing Clip Library functionality preserved; every bin item has real
  source or original reference and recorded permission state.
- Reproducible tests for cross-channel denial, idempotency, metadata integrity,
  rights revocation, SSRF, redirects and interrupted fetches when applicable.
- Unknown rights do not promote items to use in a render; denied/unavailable
  references remain visible and actionable.
- Export includes only permitted physical media plus useful durable links;
  no secrets or transient signed URLs.
- Branch, SHA, PR, changed paths, exact test commands/CI, risks, next handoff.

Do not merge, deploy, rewrite history or push directly to `main`.
