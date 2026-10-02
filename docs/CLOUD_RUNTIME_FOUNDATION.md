# Hosted Katcha control-plane foundation

Audited against accepted `main` at `2ec7fc9ae556d923f2a8266f798176be8dc6d110` on October 2, 2026.

This document is the implementation checkpoint for moving Katcha away from a local-machine availability dependency. It does **not** claim that OCI, Cloudflare, or AWS resources have been provisioned yet.

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

## Deliberately deferred before production cutover

The following are not optional; they are simply later implementation phases:

1. centralized complete workflow recovery registry and side-effect idempotency tests,
2. Cloudflare R2 migration tooling and object verification,
3. OCI Terraform/OpenTofu for A1, network, block volume, IAM and Vault,
4. Vault-to-`/etc/katcha/katcha.env` secret loader,
5. Cloudflare Tunnel,
6. independent Cloudflare Worker health watchdog + GitHub OCI recovery workflow,
7. on-demand AWS Fargate Spot analysis backend,
8. PostgreSQL point-in-time backup to R2 and tested restore,
9. production deployment workflow using immutable image SHAs,
10. VM reboot/replacement and local-PC-off chaos acceptance.

Until those gates pass, the existing installation remains the authoritative production candidate and no local data/volumes should be deleted.
