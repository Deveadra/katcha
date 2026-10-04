# Hosted Katcha control-plane foundation

Originally audited against accepted `main` on October 2, 2026. Updated October 4, 2026 after the resilience implementation series through the live-provisioning milestone.

This document describes the hosted runtime foundation. The repository now implements the recovery, fencing, backup, edge-security, break-glass, and spend-control layers that were originally deferred. It still does **not** claim that the user's live OCI, Cloudflare, R2, or AWS account resources have been provisioned. See `docs/LIVE_CLOUD_CUTOVER.md` for the ordered account-side cutover.

The production objective is **not** "Oracle Free Tier is Katcha's production machine." The production objective is: **OCI Always Free is Katcha's normal $0 control plane, with a tightly bounded paid/credited escape hatch when free capacity cannot be restored.** Free capacity is an optimization; continuity, fencing, durable state, and spend limits are correctness requirements.

## What this foundation changes

- Production runtime images used by the OCI control plane are built for both `linux/amd64` and `linux/arm64`.
- Pull-request builds now exercise the requested architectures instead of only verifying an old public `main` image.
- `deploy/docker-compose.production.yml` is a separate hosted topology. It never builds on the production host and requires immutable SHA-tagged GHCR images.
- PostgreSQL binds to `/srv/katcha/postgres`; a missing durable mount is a hard startup failure.
- No PostgreSQL or Temporal port is published.
- The API binds only to loopback pending the Cloudflare Tunnel infrastructure phase.
- MinIO is not part of hosted production. Katcha uses its existing S3-compatible storage abstraction against Cloudflare R2.
- Rendering is required to use the existing Remotion Lambda backend. The bounded renderer container remains a dispatch/poll gateway; video encoding is not allowed to run locally in hosted production.
- Heavy local analysis is behind the explicit `local-heavy-analysis` profile and is disabled by default until the on-demand external-analysis backend is implemented.
- Hosted AWS authentication is normalized under `/etc/katcha/aws`, mounted read-only into the renderer gateway. This avoids depending on the local launcher's host-specific AWS paths.
- A systemd service starts the production compose supervisor at boot and requires both the durable volume and a readable hosted AWS profile.
- Production configuration is fail-closed against known local defaults and placeholders.

## Current runtime inventory

### Images and architecture

| Image | Runtime role | OCI ARM64 requirement |
| --- | --- | --- |
| `katcha-control` | API, intelligence worker, Telegram, migrations | required |
| `katcha-ingest` | ingest + publishing worker | required |
| `katcha-analysis` | current heavy analysis worker; disabled by default on OCI | built for validation/future use |
| `katcha-production` | production + long-form orchestration | required |
| `katcha-renderer` | Remotion Lambda dispatch/poll gateway | required |
| `katcha-minio` | local development object storage | not required in hosted production |

PostgreSQL 16 Alpine and Temporal's auto-setup image are upstream multi-platform images. The production rollout must still pull and smoke-test the exact pinned tags on an A1 host before cutover.

### Active task queues and workflows

The current consolidated production runtime registers:

| Queue | Workflows |
| --- | --- |
| `katcha-media` | `ClipIngestWorkflow` |
| `katcha-publishing` | `YouTubePublicationWorkflow`, `YouTubePackagingActivationWorkflow`, `YouTubeReachSyncWorkflow`, `YouTubeAnalyticsWorkflow`, `YouTubeAnalyticsRefreshWorkflow` |
| `katcha-analysis` | `ClipAnalysisWorkflow` |
| `katcha-production` | `ShortProductionWorkflow`, `RankedShortEpisodeEditorialWorkflow`, `StagedBrandPreviewWorkflow` |
| `katcha-longform` | `LongformCompilationWorkflow` |
| discovery queue | `CommandSourcePrepareWorkflow`, `DiscoveryRunWorkflow`, `TopicWatchWorkflow`, `TopicWatchScheduleWorkflow`, `AutomaticResearchWorkflow` |
| trend queue | `ChannelTrendRefreshWorkflow`, `ChannelTrendCalibrationWorkflow` |
| intelligence queue | `CommandGoalWorkflow`, `ChannelIntelligenceRefreshWorkflow`, `ChannelIntelligenceScheduleWorkflow`, `ChannelTrendActivationWorkflow`, `ChannelTrendActivationPerformanceWorkflow`, `ChannelTrendActivationScheduleWorkflow` |

Legacy `discovery_worker.py`, `publishing_worker.py`, `longform_worker.py`, and `trend_worker.py` still exist, but the launcher marks the split worker services as retired and the consolidated workers own those queues.

### Existing recovery coverage

The repository already reconciles several persisted active states on worker startup:

- ingest and publication work in the main worker
- queued/running clip analysis
- short productions
- ranked short editorial work
- long-form compilations
- resumable discovery source runs
- automatic research workflow creation

This is **partial coverage**, not the final recovery contract. Packaging, reach, analytics, brand-preview, command-goal, topic-watch, intelligence schedules, trend schedules/activation, and every external side-effect boundary still require explicit classification in the next recovery PR. The next PR must add a registry/coverage test so adding a Temporal workflow without a recovery classification fails CI.

## External side-effect inventory

Recovery must reconcile before repeating these effects:

- source downloads and canonical-media writes
- Cloudflare R2 object creation/move/delete
- AI/TTS provider calls that can incur usage
- Remotion Lambda render dispatch
- AWS S3 render staging
- YouTube resumable upload and remote video creation
- YouTube metadata/thumbnail updates
- YouTube analytics/reporting jobs
- Telegram sends where duplicate operator messages matter

A Temporal retry is not, by itself, proof that these operations are safe to repeat.

## Durable state

Production authority is split deliberately:

- PostgreSQL: application state + Temporal persistence
- `/srv/katcha/postgres`: primary PostgreSQL files on OCI block storage
- `/srv/katcha/handoff`: durable handoff spool during the migration period
- Cloudflare R2: canonical production media and, in a later PR, off-provider database backups
- OCI Vault: production secrets in the infrastructure phase

No production cutover is allowed while an irreplaceable value exists only on the operator's local PC.

## Hosted startup path

`katcha.service` runs `production-supervisor.sh`.

The supervisor:

1. refuses startup unless `/srv/katcha` is a real mount,
2. refuses startup when durable subdirectories are missing,
3. requires a readable hosted AWS profile at `/etc/katcha/aws/config`,
4. validates the production environment without printing secrets,
5. validates the compose graph,
6. pulls exact release-SHA images,
7. starts Compose in the foreground so systemd can supervise it.

Docker restart policies recover individual services. systemd recovers the compose supervisor. The later external watchdog recovers the VM itself.

## Implementation status at the live-provisioning milestone

The items originally listed here as deferred are now implemented in the repository:

- complete workflow recovery registry and side-effect classification,
- external deployment-epoch leadership fencing,
- SQLite Durable Object recovery coordination,
- same-AD and cross-AD OCI recovery,
- bounded paid OCI fallback with durable spend reservations,
- immutable R2 PostgreSQL/Temporal backups and tested restore,
- off-OCI encrypted break-glass escrow,
- Cloudflare Tunnel/Access/WAF/rate-limit contracts,
- retired-volume cleanup safeguards,
- Remotion Lambda spend fencing,
- change-aware CI for the cloud runtime.

What remains is **live provider provisioning and controlled acceptance**, not another speculative application-architecture phase.

The repository still lacks checked-in Terraform/OpenTofu for the initial OCI VCN/subnets/A1 VM/block volume/Vault/dynamic-group bootstrap. Those resources must be provisioned in the user's OCI account before the automated recovery paths can operate.

Do not delete the local installation or any local recovery data until the complete acceptance sequence in `docs/LIVE_CLOUD_CUTOVER.md` passes, including the local-PC-off test.

