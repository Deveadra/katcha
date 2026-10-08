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

The separate `CLOUDFLARE_API_TOKEN` used by `katcha.ops.cloudflare_edge`
must have the zone permissions needed by every managed phase. In addition to
the existing Zone WAF write access for custom/rate-limit rules, the health-probe
configuration rule requires **Config Settings Write** so Katcha can disable
Browser Integrity Check only for the exact public health paths. Do not disable
Browser Integrity Check globally.

Then apply the Katcha-owned WAF/rate-limit/configuration rules:

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

Live break-glass GitHub configuration recorded 2026-10-04:

- repository secrets `KATCHA_BREAK_GLASS_R2_ACCESS_KEY`,
  `KATCHA_BREAK_GLASS_R2_SECRET_KEY`, and
  `KATCHA_BREAK_GLASS_ESCROW_KEY` are configured,
- repository variables for the R2 endpoint, bucket, handoff prefix, escrow
  prefix, region, path-style mode, and 1800-second handoff TTL are configured,
- the escrow key is also retained in a protected operator-controlled local file,
- `KATCHA_BREAK_GLASS_ESCROW_OBJECT_KEY` intentionally remains unset until a
  real encrypted escrow is published from the final production environment and
  AWS bootstrap bundle.

Live R2 acceptance recorded 2026-10-04:

- `katcha-media-prod`, `katcha-backup-prod`, and
  `katcha-break-glass-prod` were created independently in ENAM using Standard
  storage,
- public `r2.dev` access is disabled for all three buckets,
- `postgres/` in the backup bucket has a 30-day bucket lock,
- `postgres/` has a 60-day lifecycle expiry,
- `bootstrap-handoff/` in the break-glass bucket expires after 24 hours while
  `escrow/` is not covered by that rule,
- the media runtime credential proved read/write access only to the media
  bucket,
- the backup writer proved read/write access only to the backup bucket,
- the restore credential proved read-only access only to the backup bucket,
- the break-glass credential proved read/write access only to the break-glass
  bucket,
- every tested cross-bucket operation was denied and all validation objects
  were cleaned up successfully.

The temporary R2 configuration-admin credential is no longer required after
these policies are verified and should be revoked before continuing.

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

If Always Free A1 capacity is unavailable during the first deployment, enable
`.github/workflows/oci-bootstrap-capacity.yml` with
`KATCHA_OCI_BOOTSTRAP_POLL_ENABLED=true`. GitHub's shortest supported
scheduled-workflow interval is five minutes, but Katcha does not rely on that
interval as its active search loop. After a complete AD1→AD2→AD3 miss, the
workflow immediately dispatches the next serialized `mode=poll` pass. The
five-minute GitHub cron is retained as a same-provider backstop, but it is not
treated as independent recovery because a GitHub Actions outage can affect both
the active chain and its cron. Chained `mode=poll` runs still require
`KATCHA_OCI_BOOTSTRAP_POLL_ENABLED=true`, so disabling that variable is a hard
stop even if a continuation was already queued. Explicit operator
`mode=validate` runs remain available while polling is disabled.

An independent Cloudflare Durable Object dead-man watchdog covers loss of the
GitHub polling chain. Every bootstrap run records a best-effort heartbeat with
the recovery coordinator at start and completion. While bootstrap polling is
enabled, the Durable Object wakes every five minutes; if no heartbeat has been
seen for 15 minutes, it sends a `katcha-bootstrap-poll`
`repository_dispatch` through the already-authorized recovery GitHub
dispatcher. Rescue dispatches are rate-limited to one attempt per 30 minutes so
a long GitHub-hosted-runner outage cannot build an unbounded queue. When a
Cloudflare rescue run actually starts, GitHub sends the operator a best-effort
Telegram "polling recovery" notice. The watchdog automatically disables when
the acquisition handoff completes. A Cloudflare outage does not stop the primary
GitHub self-chain because heartbeat delivery is intentionally best-effort.

After deploying the updated recovery Worker, enable this independent watchdog:

```bash
cd ~/src/katcha
git pull --ff-only origin main
npx --yes wrangler@4.146.0 deploy --config infra/cloudflare-recovery/wrangler.jsonc
bash scripts/configure-cloudflare-bootstrap-watchdog.sh
```

The configurator prompts privately for the recovery admin token when it is not
already present in `KATCHA_RECOVERY_ADMIN_TOKEN`; it does not print the token.
This avoids an artificial five-minute idle gap while still guaranteeing that
provider-mutating attempts never overlap. The workflow keeps the full
2-OCPU/12-GB A1 target, never selects a paid shape, and creates/attaches the
50-GB durable volume only after compute placement succeeds. It reconciles an
uncertain launch response by searching for the exact production instance before
trying another AD, which prevents duplicate instances after provider/API
timeouts.

When capacity is acquired, the workflow performs an explicit handoff before
stopping the search: it records the winning instance/AD/subnet/volume as the
new primary GitHub recovery target, recomputes the two alternate AD targets,
sends the operator a required Telegram "capacity acquired" notification, and
only then sets `KATCHA_OCI_BOOTSTRAP_POLL_ENABLED=false`. If metadata
persistence or Telegram delivery fails, polling remains enabled; the next run
reuses the already-created instance and retries the handoff rather than launching
a duplicate. The poller is also skipped once
`KATCHA_OCI_RECOVERY_CONFIGURED=true`.

The host should not expose PostgreSQL, Temporal, or the Katcha API directly to
the public internet.

### After A1 capacity is acquired

Do **not** rerun `scripts/configure-github-oci-bootstrap.sh` after a successful
acquisition. The winning AD/subnet/volume becomes the production-primary
placement, and the configurator now refuses to overwrite that state.

The first acquired Ubuntu/Ampere host intentionally has no public IP. Use OCI
Bastion **SSH port forwarding**, not Managed SSH, for the one-time foundation
bootstrap. Oracle documents a Managed-SSH limitation for Ampere A1 instances
running Ubuntu; port-forwarding sessions do not require the Bastion agent plugin.

The dedicated recovery identity needs a temporary elevation for this initial
administrative path because creating the service gateway, route, NSG, and VNIC
membership requires network-management permissions. Add these statements to the
existing root-tenancy Katcha policy for the bootstrap:

```text
Allow group katcha-github-recovery to manage bastion-family in compartment katcha-prod
Allow group katcha-github-recovery to manage virtual-network-family in compartment katcha-prod
Allow group katcha-github-recovery to inspect work-requests in tenancy
```

After the foundation bootstrap succeeds, downgrade the network grant back to the
normal recovery permission:

```text
Allow group katcha-github-recovery to use virtual-network-family in compartment katcha-prod
```

The Bastion grant can remain while private emergency administration is needed,
or later be narrowed to session-only access once the operator-access path is
fully finalized.

Then inspect the planned changes without mutating OCI:

```bash
cd ~/src/katcha
git pull --ff-only origin main
bash scripts/bootstrap-acquired-oci-host.sh
```

The script verifies that:

- GitHub records capacity as acquired and polling is disabled,
- the recorded primary instance is RUNNING at exactly 2 OCPU / 12 GB,
- the VNIC has no public IP and is in the recorded winning subnet,
- the recorded durable block volume is ATTACHED to that exact instance,
- the operator source address can be restricted to one IPv4 /32.

When run with `--apply`, it idempotently:

1. creates/reuses the regional OCI service gateway and appends its route without
   replacing the existing NAT route,
2. creates/reuses a free OCI Bastion restricted to the operator's current /32,
3. creates a target-specific NSG that permits TCP/22 only from the Bastion
   private endpoint and attaches that NSG only to the production VNIC,
4. uses a three-hour Bastion port-forwarding session to reach the Ubuntu host,
5. runs `deploy/scripts/bootstrap-initial-primary.sh` as root,
6. formats the new data volume only when it has no filesystem, mounts it at
   `/srv/katcha`, installs the minimal Docker/runtime foundation, and checks
   out the exact `origin/main` SHA,
7. records the filesystem UUID and foundation-ready metadata back into GitHub.

Run:

```bash
bash scripts/bootstrap-acquired-oci-host.sh --apply
```

This stage deliberately does **not** copy production secrets, start Katcha, set
`KATCHA_OCI_RECOVERY_CONFIGURED=true`, or send the "Katcha is back online"
notification. Those remain gated on Vault, Tunnel, runtime readiness, fencing,
authority commit, and public health.

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

### OCI Vault AWS bundle size and immutable helper

OCI Vault secret content is capped at 25 KB. The hosted AWS directory also
contains the public `aws_signing_helper` executable (approximately 4 MB
compressed), which **must not** be uploaded as a Vault secret. Normal
`oci-vault` recovery now consumes an AWS credential-only `tar.gz` containing
exactly these three private files, with no enclosing directory:

- `config`
- `runtime/client.pem`
- `runtime/client-key.pem`

On a trusted host, prepare it from the protected current production files
without printing their contents:

```bash
sudo install -d -m 0700 /root/katcha-recovery-vault-stage
sudo python3 scripts/pack-oci-aws-vault-credentials.py \
  --aws-dir /etc/katcha/aws \
  --output /root/katcha-recovery-vault-stage/aws-credentials.tgz
```

The packer refuses missing/symlinked files, an existing output file, and
archives over 24,000 bytes. Run it from a checkout containing the packer, or
stream the reviewed packer script over the protected Bastion connection. Do not
read or print the resulting bytes in logs, chats, or CI output. Provision the
small archive as the `KATCHA_OCI_AWS_BUNDLE_SECRET_ID` Vault secret using a
separate authorized Vault operator identity; the restricted GitHub recovery
identity does not need Vault access.

During OCI-Vault recovery, cloud-init checks the archive's three exact members,
extracts only the private files, then downloads the official AWS IAM Roles
Anywhere credential helper version 1.8.5 for the candidate CPU architecture
from `rolesanywhere.amazonaws.com`. The download is verified against AWS's
published platform-specific SHA-256 before installation. Recovery fails closed
if HTTPS to AWS is unavailable, the digest differs, or the bundle contains
unexpected files. Before enabling autonomous recovery, verify the candidate's
private network permits this narrowly scoped outbound HTTPS download and a
checksum-valid result. No helper binary is required in Vault.

The independent, explicitly authorized `break-glass` recovery path continues
to use its encrypted off-OCI escrow bundle and does not change.

### Vault principal separation

Do not grant the dedicated `katcha-github-recovery` API user broad Vault
inspection or secret-content permissions just to make `oci kms vault list` or
`oci vault secret list` succeed. The GitHub recovery principal launches and
manages infrastructure; it passes already-recorded secret OCIDs into cloud-init
but does not need to read secret contents.

Normal recovery reads those four secret bundles from the replacement VM with
OCI instance-principal authentication. Create a dynamic group such as
`katcha-recovery-candidates` with this matching rule:

```text
instance.compartment.id = '<katcha-prod compartment OCID>'
```

Then grant only secret-bundle read access:

```text
Allow dynamic-group katcha-recovery-candidates to read secret-bundles in compartment katcha-prod
```

This is the permission used by
`oci secrets secret-bundle get --auth instance_principal` in the recovery
cloud-init. If `katcha-prod` ever contains unrelated compute instances, replace
the compartment-wide dynamic-group rule with a dedicated recovery compartment
or a defined-tag rule before enabling autonomous recovery.

Create the Vault, symmetric encryption key, and the four secrets with a separate
operator/administrator identity that is authorized to administer Vault. Record
only the resulting `ocid1.vaultsecret...` identifiers in GitHub repository
variables. Listing Vaults or Secrets with the restricted GitHub recovery API
user is not a valid recovery-readiness test.

## Phase 6 — GitHub recovery credentials

Use a dedicated OCI identity for GitHub recovery rather than a personal
administrator API key. In the Default identity domain create a group named
`katcha-github-recovery`, place the dedicated service user in that group, and
attach a tenancy policy with only:

```text
Allow group katcha-github-recovery to manage instance-family in compartment katcha-prod
Allow group katcha-github-recovery to manage volume-family in compartment katcha-prod
Allow group katcha-github-recovery to use virtual-network-family in compartment katcha-prod
Allow group katcha-github-recovery to read instance-images in tenancy
Allow group katcha-github-recovery to read app-catalog-listing in tenancy
```

Because this group is in the Default identity domain, the domain qualifier may
be omitted; OCI treats the unqualified group name as `Default/katcha-github-recovery`.
Create this policy in the root compartment so the tenancy-scoped image/catalog
reads and the `katcha-prod` grants can live together.

The GitHub workflows validate OCI authentication by listing instances only in
`katcha-prod`; they do not require tenancy-wide region-subscription access.
They also do not require the `katcha-github-recovery` API user to list Vaults,
list Secrets, or retrieve secret bundles. Secret contents are retrieved later
by the recovery candidate's instance principal.

Configure repository secrets required by the OCI recovery workflow:

- `OCI_TENANCY_OCID`
- `OCI_USER_OCID`
- `OCI_FINGERPRINT`
- `OCI_API_PRIVATE_KEY`
- `KATCHA_GITHUB_AUTOMATION_TOKEN` — fine-grained PAT scoped only to this
  repository with repository **Variables: read/write** and **Actions:
  read/write**. It does not need Secrets write; the operator's local
  authenticated `gh` session installs secrets,
- `KATCHA_TELEGRAM_BOT_TOKEN`,
- `KATCHA_TELEGRAM_CHAT_ID`
- `KATCHA_RECOVERY_ADMIN_TOKEN`
- `KATCHA_RECOVERY_CANDIDATE_TOKEN`
- `KATCHA_EXTERNAL_COMPUTE_TOKEN`
- break-glass R2 access key/secret,
- break-glass escrow key.

Configure all current repository variables referenced by
`.github/workflows/oci-recovery.yml` and
`.github/workflows/oci-bootstrap-capacity.yml`, including:

- OCI region/compartment/AD/subnet/image settings,
- alternate-AD target JSON,
- `KATCHA_OCI_SSH_PUBLIC_KEY` for the initial private A1 host,
- `KATCHA_OCI_BOOTSTRAP_POLL_ENABLED` while first-placement polling is needed,
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

Telegram notifications are deliberately emitted from GitHub rather than the OCI
host so loss of the host cannot suppress recovery alerts. The Bot API
`sendMessage` call requires a bot token and target chat ID. The operator must
start/contact the bot at least once before a bot can send a private message.
Optionally set repository variable `KATCHA_TELEGRAM_THREAD_ID` when delivering
into a Telegram forum topic. Acquisition notifications mean only that compute
and durable storage were secured; they do not claim Katcha is serving traffic.
The production recovery workflow sends a separate "Katcha is back online"
message only after authority commit and a fresh public-health check succeed.

Use `scripts/configure-github-recovery-notifications.sh` to install the
automation PAT and Telegram credentials without echoing them. The script proves
the PAT can mutate repository variables, uses that PAT to dispatch the manual
Telegram acceptance workflow, and waits for the real message-delivery test to
pass. Do this before enabling capacity polling.

For the live Ashburn bootstrap, `scripts/configure-github-oci-bootstrap.sh`
installs the known AD/subnet/image/volume/SSH settings and the four OCI API
credentials through `gh` without printing the private key. Run it once without
`--enable` to stage configuration, then rerun with `--enable` after the
dedicated OCI API key has been uploaded and its fingerprint verified. After an
acquisition transition records `KATCHA_OCI_BOOTSTRAP_ACQUIRED=true`, the
configurator fails closed rather than resetting the winning placement.

Keep `KATCHA_OCI_RECOVERY_CONFIGURED` unset or `false` while live OCI
variables/secrets are incomplete. Set it to `true` only after the recovery
configuration is fully populated and validated. Once enabled, GitHub Actions
provides an independent five-minute watchdog backstop that calls the recovery
coordinator's authenticated `/v1/watchdog/probe-now` endpoint. The coordinator
remains the single incident/dispatch authority, so this backstop reuses the same
failure threshold, idempotent incident state, and serialized recovery workflow
rather than creating a competing recovery path. The separate fifteen-minute
schedule continues to enforce paid-fallback TTL and retired-volume cleanup.

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

### Initial media handoff

The PostgreSQL cutover does not by itself move objects that still exist only in
the local MinIO bucket. Before leadership is initialized, compare the frozen
local media bucket with the production R2 media bucket:

```bash
cd ~/src/katcha
python scripts/verify_media_cutover.py
```

The default invocation is read-only. It verifies that the target is the expected
`katcha-media-prod` Cloudflare R2 bucket and reports the exact number/bytes of
objects that are absent, size-mismatched, or content-mismatched. Same-size
objects are compared by streaming SHA-256 so a silent content mismatch cannot
pass the cutover check. It never treats target-only objects as a reason to delete
anything.

If the plan reports objects to copy, keep all local mutating workers stopped and
run:

```bash
python scripts/verify_media_cutover.py --apply
python scripts/verify_media_cutover.py
```

The apply mode copies only objects that fail the comparison, verifies each
copied object's size and SHA-256, and performs a complete second comparison
before reporting success. Keep the local MinIO data intact as part of the
pre-cutover rollback source.

## Phase 8 — initialize leadership

Initial coordinator state starts with active epoch 0.

For the first live cutover, prefer the checked-in fail-closed operator wrapper.
It keeps recovery-admin credentials on the operator workstation, requires a
usable Bastion TCP+SSH path, verifies that only the restored PostgreSQL candidate
is running, pins the host and environment to an explicit release SHA, runs both
production validators, proves local/public/durable/fence readiness, reports
candidate readiness, and reconciles an uncertain commit response before taking
any destructive action.

Run the inspect-only pass first:

```bash
cd ~/src/katcha
git pull --ff-only origin main
RELEASE_SHA="$(git rev-parse HEAD)"
python scripts/oci_production_cutover.py activate-primary \
  --release-sha "$RELEASE_SHA"
```

The inspect-only pass must also authenticate the read-only fencing endpoint
using the effective `KATCHA_FENCE_TOKEN`. A 401 indicates the operator's token
does not match `FENCE_TOKEN` on the deployed Cloudflare recovery Worker; do not
run `--apply` until that is repaired. The cutover uses the exported environment
first, then the local `.env`, then the protected
`~/.config/katcha/production/recovery-fence-token` file. Never display or
log the token contents.

Only after that passes, apply the same exact release:

```bash
python scripts/oci_production_cutover.py activate-primary \
  --release-sha "$RELEASE_SHA" \
  --apply
```

The lower-level coordinator protocol remains documented below for recovery and
manual diagnosis.

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

Use a conservative failure threshold first. The production default is a
60-second probe interval, three consecutive failures, and a 300-second
unresolved-recovery redispatch interval. The independent GitHub Actions
backstop also probes every five minutes, but both paths reuse the same Durable
Object incident state and therefore cannot independently authorize competing
recoveries.

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
