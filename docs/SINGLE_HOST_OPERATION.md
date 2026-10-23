# Single-host operation

VM1 can run the existing acquisition worker alongside Katcha. The acquisition
queue, scoped permissions, extractor transport, private journal, multipart resume,
and primary-side media validation are the same whether that worker runs on VM1 or
VM2. `remote_acquisition` names the worker protocol, not a physical-host requirement.
Local execution does not fix provider rejection of the VM's outbound route.

## Resource scheduling

`KATCHA_LOCAL_DEGRADED_MODE=true` enables a host-local resource budget for registered
media activities. All participating containers must mount the **same local Docker
volume** at `/resource-budget` and set `KATCHA_LOCAL_RESOURCE_LOCK_PATH` to
`/resource-budget/host.lock`. This is a single-host budget, not distributed leadership.
Never delete or replace the lock file while services run; that can split exclusion.

The acquisition worker acquires the budget before claiming a job. If busy, it leaves
the job queued and tries again on a later poll. Temporal media activities make a
side-effect-free admission attempt; if busy, their workflows wait on a durable
15-second timer. Waiting does not consume provider retries or the operation's
execution timeout. Permission failures and uncertain provider outcomes retain their
existing recovery behavior; they are never retried as resource contention.

Waiters have stable FIFO tickets. Active waiters refresh their ticket on each attempt;
cancelled or stopped waiters expire after two minutes, so they cannot block the host
forever. A restarted waiter may lose its old position. The operating system releases
the execution lock on process exit. Temporal and the acquisition job database retain
the actual work; ticket files are scheduling hints, not authoritative job records.

The budget covers queued acquisition, local intelligence/vision, measured scene
indexing, narration preparation, media preparation, rendering, and video uploads.
API requests, workflow timers, database services, discovery, and lightweight status
operations continue independently. Each Temporal worker polls at most one activity
at a time in this mode. Shared worker processes retain two executor threads so
separate queues can make progress. See `resource_queue.MEDIA_ACTIVITIES` for the
exact admitted activity set. Direct renderer calls, browser sessions, and unmanaged
processes are outside this gate; they must not be used for parallel heavy work during
acceptance. Existing provider budgets and paid-operation approval remain applicable.

## Production profile

`deploy/docker-compose.single-host.yml` is an opt-in overlay for
`deploy/docker-compose.production.yml`. It uses the same immutable release SHA,
starts a scoped acquisition worker, mounts the shared budget in participating workers,
and selects local rendering with one render thread. Renderer limits increase to
3 GiB / 1.25 CPUs; acquisition is bounded to 1 GiB / 1 CPU. These are ceilings, not
reservations. Actual peak memory and disk usage still need measurement on VM1.

Prepare a dedicated acquisition env file containing its trusted HTTPS control URL
and the **actual Forescene channel UUID**, not the display name. Supply any required
Cloudflare Access credential file paths and the control CA path using the existing
worker configuration. The worker gets no core database or bucket credentials.
`KATCHA_ACQUISITION_ENV_FILE` points to that file;
`KATCHA_ACQUISITION_CREDENTIAL_DIR` points to a private directory containing
`worker-token` and any separately required Access files. The token must belong to a
channel-scoped worker principal; preserve the existing operator principal. Files
must be readable only by UID 10001. Do not copy the core env file into this service.

The profile initializes persistent journal and budget volume ownership without
network access. Keep `local-acquisition-state` on durable storage with enough free
space: the existing worker requires up to three times the configured acquisition
byte limit for extraction/merge, plus its reserve. A low-disk rejection should remain
visible; increasing concurrency is not a storage remedy.

Before activation:

1. Pass the existing release, backup/restore, and hosted deployment gates for the
   exact release. The supervisor selects the overlay only when the core env file
   contains exactly one `KATCHA_SINGLE_HOST_PROFILE_ENABLED=true`. The immutable
   release verifier then requires the acquisition and analysis services and stages
   the additional ARM64 analysis image. Default deployments remain unchanged.
   First activation must be prepared before upgrading from a release without the
   profile marker; the deployer requires no running workflows for that transition.
   Changing the profile on an already running release is not a supported hot toggle.
2. Pause new media intake and drain existing media activities before enabling the
   budget. Old workflow histories retain original activity names for replay; already
   scheduled legacy activities do not acquire the new budget. All relevant workers
   must run the release together before intake resumes.
3. Validate the combined Compose configuration privately with `config --quiet`.
   Include `--profile local-heavy-analysis`, or analysis jobs have no local consumer.
4. Verify the API's worker principal and HTTPS/Access route, then start the profile
   through the supervised deployment path. Keep the existing recovery fencing.
5. Inspect resource measurements during the first project before increasing job size.

## Forescene acceptance

Create a separate Forescene project for this test; preserve existing projects.
Acceptance requires recorded live evidence for each step:

- Source discovery returns an actual usable candidate and a valid acquisition permission.
- VM1 claims the job, downloads permitted bytes, and reports them through verified
  handoff; the resulting Clip and project media belong to Forescene.
- A simultaneous render/acquisition attempt waits without duplicate provider calls.
- Analysis, project assembly, and local rendering produce a playable inspected video.
- Scheduled publishing preserves the intended schedule and exact video identity,
  using the channel's existing publication approval rules. Verify the provider's
  upload/video ID before claiming completion.
- Interrupt the worker during a recoverable transfer, restart it, and verify exact
  resume or explicit reconciliation rather than an uncontrolled second download.

Record wall time, peak RAM, disk free space, bytes transferred, queue wait, and any
missed publication deadline. Finishing slowly is acceptable; claiming that a queued
job has published, or silently dropping a missed schedule, is not.

Current offline tests establish scheduling/exclusion and existing worker semantics.
They do not establish live source access, successful VM1 rendering, a new deployed
release, or a scheduled public upload. Core-role switching between VMs remains a
separate database/workflow recovery and fencing feature.
