# Katcha live cloud cutover

Status: **code-side cloud resilience complete; live provider provisioning and acceptance remain.**

This runbook is the handoff between the completed repository implementation and
the first real 24/7 hosted Katcha deployment.

It does **not** claim that OCI, Cloudflare, R2, or AWS resources already exist.
Do not delete the current local installation, local databases, local media, or
working credentials until every acceptance gate at the end of this document has
passed.

The target architecture is:

The current live provisioning selections are:

- OCI tenancy: `AerithDimensional`, identity domain `Default`, home region `us-ashburn-1`,
- Cloudflare zone: `katcha.stream`,
- production application hostname: `app.katcha.stream`,
- recovery coordinator hostname: `recovery.katcha.stream`.

- OCI Ampere A1 Always Free as the normal near-$0 control plane,
- Cloudflare Tunnel as the only application ingress,
- Cloudflare Access/WAF/rate limiting at the edge,
- Cloudflare Durable Object as the external leadership/recovery/budget authority,
- Cloudflare R2 for canonical media, immutable disaster backups, and encrypted
  off-OCI break-glass escrow,
- OCI block volume for the active PostgreSQL/Temporal data directory,
- Remotion Lambda for rendering,
- bounded paid OCI fallback only after configured free A1 recovery options fail,
- the local PC removed from production availability after acceptance.

## Milestone boundary

Merged repository support now includes:

- ARM64 hosted runtime and production Compose topology,
- complete workflow recovery classification,
- external mutation leadership fencing,
- strongly consistent recovery coordinator,
- serialized OCI recovery workflow,
- same-AD A1 replacement,
- alternate-AD A1 recovery from R2,
- bounded paid OCI fallback,
- stale-host fencing,
- immutable R2 database backups and weekly restore verification,
- retired-volume grace/cleanup,
- off-OCI encrypted break-glass escrow and drill,
- Cloudflare Access/WAF/rate-limit configuration,
- durable external-compute budget ledger,
- spend reservation before paid OCI/Lambda dispatch,
- change-aware CI and current-main validation.

The repository does **not** currently contain an OCI Terraform/OpenTofu module
that creates the initial VCN/subnets/A1 VM/block volume/Vault/dynamic group.
Initial OCI infrastructure is therefore an explicit operator provisioning step.
Recovery after that initial bootstrap is automated by the checked-in recovery
workflow.

## Phase 0 — freeze a known-good release

1. Work from the accepted `main` branch.
2. Record the exact release SHA that will be deployed.
3. Confirm GitHub CI is green for that SHA.
4. Keep the current local installation available as the rollback source.
5. Do not let local and cloud workers process the same production queues at the
   same time.
6. Do not rotate/delete current OAuth/provider credentials during the move.
7. Do not delete any local database, media, Git history, or recovery material.

The production environment must use the exact 40-character release SHA in
`KATCHA_RELEASE_SHA`.

## Phase 1 — Cloudflare recovery authority

Deploy the checked-in Worker/Durable Object:

```bash
npx --yes wrangler@4.146.0 deploy \
  --config infra/cloudflare-recovery/wrangler.jsonc
```

Install five independent secrets into that Worker:

- `FENCE_TOKEN`
- `RECOVERY_ADMIN_TOKEN`
- `RECOVERY_CANDIDATE_TOKEN`
- `RECOVERY_DISPATCH_TOKEN`
- `EXTERNAL_COMPUTE_TOKEN`

Use separate random values. Never reuse the Katcha operator/control token.

The checked-in Worker dispatches recovery incidents to:

`https://api.github.com/repos/Deveadra/katcha/dispatches`

The GitHub dispatch token should be scoped only to this repository and only to
the permission needed to create repository-dispatch events.

Before continuing, verify:

```bash
curl -fsS https://<recovery-worker-host>/healthz
```

Expected result: HTTP 200 and an `ok: true` recovery-coordinator response.

Live acceptance recorded 2026-10-04:

- Worker `katcha-recovery-coordinator` deployed successfully,
- Custom Domain `recovery.katcha.stream` attached,
- Durable Object `RecoveryAuthority` created,
- all five Worker secrets bound,
- public `/healthz` returned `{"ok":true,"service":"katcha-recovery-coordinator"}`,
- authenticated `GET /v1/authority/status` returned version 1 with
  `max_epoch: 0`, no active or pending deployment, watchdog disabled, and no
  active incident.

The live Cloudflare recovery authority is therefore accepted. A local WSL DNS
resolver issue observed during validation is not a Cloudflare-side blocker
because both Cloudflare and Google public resolvers returned the Custom Domain
addresses and direct TLS/HTTP validation through Cloudflare succeeded.

Set GitHub repository variables/secrets used by
`.github/workflows/oci-recovery.yml`, including:

- `KATCHA_RECOVERY_COORDINATOR_URL`
- `KATCHA_RECOVERY_ADMIN_TOKEN`
- `KATCHA_RECOVERY_CANDIDATE_TOKEN`
- `KATCHA_EXTERNAL_COMPUTE_TOKEN`

## Phase 2 — Cloudflare application edge

Choose the final production hostname before configuring OAuth, Tunnel, or Access.

Create a Cloudflare Tunnel for Katcha and record its tunnel token for
`KATCHA_CLOUDFLARE_TUNNEL_TOKEN`.

The production API remains loopback-only on the VM. Cloudflare Tunnel is the
public origin path.

Apply Access configuration from `infra/cloudflare-edge`:

```bash
terraform -chdir=infra/cloudflare-edge init

terraform -chdir=infra/cloudflare-edge plan \
  -var='cloudflare_zone_id=<zone-id>' \
  -var='katcha_hostname=<katcha.example.com>' \
  -var='operator_emails=["<operator@example.com>"]'

terraform -chdir=infra/cloudflare-edge apply
```

Supply `cloudflare_api_token` securely rather than committing it.

Then apply the Katcha-owned WAF/rate-limit rules:

```bash
export CLOUDFLARE_ZONE_ID='<zone-id>'
export CLOUDFLARE_API_TOKEN='<edge-rules-token>'
export KATCHA_PUBLIC_HOSTNAME='<katcha.example.com>'
export KATCHA_PUBLIC_RATE_LIMIT_REQUESTS_PER_10S='30'

python -m katcha.ops.cloudflare_edge apply
python -m katcha.ops.cloudflare_edge check
```

Acceptance here is:

- human/operator UI is blocked without Cloudflare Access,
- approved operator identity can enter,
- public health endpoints remain reachable,
- YouTube OAuth callback remains reachable,
- `/v1/*` remains governed by Katcha bearer-principal authentication,
- sensitive probe paths are blocked.

## Phase 3 — R2 storage domains

Create **three separate storage domains**:

1. production media bucket, e.g. `katcha-media-prod`,
2. immutable database backup bucket, e.g. `katcha-backup-prod`,
3. break-glass bucket, e.g. `katcha-break-glass-prod`.

Do not collapse these into one credential/bucket.

Create scoped credentials for:

- runtime media access,
- backup writer,
- backup read-only restore,
- break-glass GitHub/workstation access.

The Cloudflare bucket-configuration/admin token must not be mounted into Katcha.

Configure backup lock/lifecycle exactly as documented in
`docs/DISASTER_RECOVERY_BACKUPS.md`.

Minimum production behavior:

- locked backup prefix,
- separate writer/reader credentials,
- 30-day immutable retention by default,
- later lifecycle expiry (60 days by default),
- actual restore test succeeds.

Configure break-glass lifecycle/escrow using
`docs/BREAK_GLASS_RECOVERY.md`.

Run the GitHub **Break-glass escrow drill** before relying on that path.

## Phase 4 — initial OCI infrastructure

Provision the initial OCI resources in the chosen home region.

Required minimum:

- VCN,
- subnet for the primary Availability Domain,
- subnet(s) for each alternate AD used by recovery,
- A1-compatible network/security rules,
- initial `VM.Standard.A1.Flex` instance,
- durable block volume for `/srv/katcha`,
- OCI Vault,
- OCI instance-principal dynamic group,
- least-privilege policies for required Vault secret reads.

Normal A1 target:

- 2 OCPU,
- 12 GB RAM.

The host should not expose PostgreSQL, Temporal, or the Katcha API directly to
the public internet.

Record:

- tenancy OCID,
- compartment OCID,
- region,
- primary AD,
- primary subnet OCID,
- alternate AD/subnet pairs,
- A1 image OCID,
- durable volume OCID,
- durable filesystem UUID.

Populate the corresponding GitHub variables consumed by
`.github/workflows/oci-recovery.yml`.

## Phase 5 — production secrets

Create the production environment from `.env.production.example`.

Important required groups include:

- release SHA,
- leadership-fence URL/token/deployment identity,
- Cloudflare Tunnel token,
- PostgreSQL credentials,
- production R2 media credentials,
- persistent Katcha credential-encryption key,
- Katcha control principals/tokens,
- YouTube OAuth configuration using the final HTTPS hostname,
- AWS/Remotion configuration,
- external-compute coordinator/token/budget ceiling.

Create:

- `/etc/katcha/katcha.env`
- `/etc/katcha/backup.env`
- `/etc/katcha/restore.env`
- `/etc/katcha/aws/*`

with restrictive permissions.

Store equivalent bootstrap material in OCI Vault for normal recovery.

The two additional Vault environment bundles are required so a replacement VM
restores disaster-backup protection automatically.

## Phase 6 — GitHub recovery credentials

Configure repository secrets required by the OCI recovery workflow:

- `OCI_TENANCY_OCID`
- `OCI_USER_OCID`
- `OCI_FINGERPRINT`
- `OCI_API_PRIVATE_KEY`
- `KATCHA_RECOVERY_ADMIN_TOKEN`
- `KATCHA_RECOVERY_CANDIDATE_TOKEN`
- `KATCHA_EXTERNAL_COMPUTE_TOKEN`
- break-glass R2 access key/secret,
- break-glass escrow key.

Configure all current repository variables referenced by
`.github/workflows/oci-recovery.yml`, including:

- OCI region/compartment/AD/subnet/image settings,
- alternate-AD target JSON,
- durable-volume identity/device path,
- backup freshness limit,
- retired-volume grace period,
- Vault secret OCIDs,
- A1 shape/OCPU/RAM,
- paid-fallback shape/OCPU/RAM,
- paid-fallback TTL/hourly estimate/per-incident ceiling,
- recovery candidate timeout,
- break-glass bucket/prefix/object pointer/TTL,
- public Katcha health URL.

Leave paid external compute disabled until the budget ledger has been configured.

Keep `KATCHA_OCI_RECOVERY_CONFIGURED` unset or `false` while live OCI
variables/secrets are incomplete. Set it to `true` only after the recovery
configuration is fully populated and validated; this enables the scheduled
paid-fallback TTL/retired-volume cleanup job without generating false failures
during provisioning.

## Phase 7 — prepare the durable host

Attach and mount the durable volume at `/srv/katcha`.

Required durable directories include:

- `/srv/katcha/postgres`
- `/srv/katcha/handoff`
- `/srv/katcha/recovery`
- `/srv/katcha/backups-local`

Install the production systemd/timer units:

```bash
cd /opt/katcha
sudo KATCHA_REPO_ROOT=/opt/katcha \
  /bin/bash deploy/scripts/install-production-units.sh
```

Validate configuration before starting:

```bash
PYTHONPATH=/opt/katcha/src \
  python3 scripts/validate_production_runtime.py \
  --env-file /etc/katcha/katcha.env

PYTHONPATH=/opt/katcha/src \
  python3 -m katcha.ops.disaster_recovery_validate \
  --production-env /etc/katcha/katcha.env \
  --backup-env /etc/katcha/backup.env \
  --restore-env /etc/katcha/restore.env
```

Do not start cloud automation while the local production workers are still
allowed to mutate production.

## Phase 8 — initialize leadership

Initial coordinator state starts with active epoch 0.

Prepare the first deployment:

```bash
curl -fsS \
  -H "Authorization: Bearer $KATCHA_RECOVERY_ADMIN_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{
    "deployment_id": "oci-a1-primary-001",
    "health_url": "https://<katcha-host>/v1/health/ready",
    "expected_active_epoch": 0
  }' \
  "$KATCHA_RECOVERY_COORDINATOR_URL/v1/authority/prepare"
```

Use the returned epoch in `KATCHA_DEPLOYMENT_EPOCH` and the same deployment ID
in `KATCHA_DEPLOYMENT_ID`.

Start Katcha:

```bash
sudo systemctl start katcha.service
sudo systemctl start katcha-backup.timer
sudo systemctl start katcha-restore-test.timer
```

Verify:

```bash
curl -fsS http://127.0.0.1:8000/v1/health/ready
curl -fsS https://<katcha-host>/v1/health/ready
```

The candidate must still be fenced before authority commit.

Report readiness with the candidate token only after all four checks pass:

- runtime ready,
- durable state ready,
- fence probe ready,
- public route ready.

Then commit with the recovery-admin token and
`expected_active_epoch: 0`.

After commit, verify the fence assertion authorizes only that deployment/epoch.

## Phase 9 — enable the watchdog

Configure watchdog recovery only after the initial leader is healthy and GitHub
recovery variables/secrets are complete.

Use a conservative failure threshold first.

Verify:

- normal health probes do not open incidents,
- repeated failed probes open exactly one incident,
- dispatch uses the single GitHub recovery concurrency group,
- recovery cannot commit an unready candidate.

## Phase 10 — configure the spend ledger

Keep external paid compute disabled until policy exists.

Configure the Durable Object ledger with explicit:

- UTC monthly ceiling,
- OCI provider ceiling,
- AWS Lambda provider ceiling,
- maximum concurrent paid jobs,
- maximum retry attempts,
- maximum retry spend.

The example in `infra/cloudflare-recovery/README.md` uses a $10 monthly ceiling
split between OCI and Lambda. Use a value you are comfortable losing in a worst
case, not a projected free-credit value.

Only after the ledger is configured should
`KATCHA_EXTERNAL_COMPUTE_ENABLED` and paid OCI fallback be enabled.

## Phase 11 — acceptance before local-PC cutover

Do not declare production ready until all of these have been observed against
real configured infrastructure.

### Normal-host acceptance

- production host reboots and returns healthy without the local PC,
- PostgreSQL/Temporal state survives,
- Cloudflare Tunnel returns,
- timers resume,
- queued/recoverable jobs continue correctly.

### Backup acceptance

- force one database backup,
- verify R2 completion marker,
- run the restore-test service,
- prove overwrite/delete against locked backup data is denied.

### Same-AD recovery

- stop/replace the A1 compute instance,
- reattach the durable volume,
- prove candidate readiness,
- commit a new epoch,
- wake the stale host and prove it cannot perform external mutations.

### Cross-AD recovery

- make the original instance/data volume unavailable to the recovery test,
- launch in a configured alternate AD,
- create fresh durable storage,
- restore from the newest acceptable R2 recovery point,
- prove PostgreSQL/Temporal/application state,
- commit only after public/fence readiness.

### Paid-fallback acceptance

- simulate A1 capacity failure,
- prove free targets are attempted first,
- prove budget reservation occurs before paid instance launch,
- prove TTL/per-incident/monthly ceilings,
- prove cleanup/settlement,
- prove superseded durable storage is retained for its rollback grace period and
  later removed only when inactive/unattached.

### Break-glass acceptance

- run the escrow drill,
- run one controlled manual recovery with `secret_source=break-glass`,
- prove the candidate reaches readiness without a successful OCI Vault read,
- prove the temporary handoff is deleted.

### Edge acceptance

- unauthenticated browser cannot reach operator UI,
- approved operator can,
- expected machine/OAuth routes work,
- Katcha API bearer auth still rejects invalid credentials,
- WAF/rate-limit rules match the checked-in contract.

### Local-PC-off acceptance

Finally:

1. stop every local production worker/service,
2. leave the local PC off,
3. run real Katcha operator actions through the public hostname,
4. observe ingestion, production, rendering and publishing,
5. verify backups and health/recovery telemetry,
6. keep the local installation intact but non-authoritative until a stable
   hosted observation window has passed.

## Rollback rules

Before authority commit:

- abort the pending coordinator epoch,
- stop/terminate the candidate,
- leave the current authoritative deployment unchanged.

After authority commit:

- never simply restart an older host and assume it is authoritative,
- every replacement must receive a newer epoch,
- rely on fencing to keep stale hosts read-only/non-mutating.

Do not delete old durable volumes manually during a recovery event. The checked-in
retirement logic keeps superseded volumes for a rollback grace window and deletes
only explicitly authorized, expired, unattached, non-active volumes.

## Definition of this milestone

This repository is at the **live-provisioning milestone** when:

- current `main` CI is green,
- no cloud-resilience implementation PR remains open,
- the resilience contract is represented in code/tests,
- operator setup/cutover order is documented,
- remaining work requires real provider accounts/credentials or live acceptance
  rather than additional speculative architecture.

That is the intended stopping point before account-side provisioning begins.
