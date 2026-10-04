# OCI recovery dispatcher

This document describes the automated recovery path used after the Cloudflare
Durable Object watchdog opens a Katcha production incident.

The design goal is **Always Free first, bounded paid continuity second**. The
dispatcher never commits a replacement merely because OCI launched an instance.

## Recovery sequence

1. Cloudflare detects the active production health endpoint failing for the
   configured threshold.
2. The Durable Object creates one incident and sends one idempotent GitHub
   `repository_dispatch` event.
3. GitHub Actions serializes all production recovery through the
   `katcha-production-recovery` concurrency group.
4. The dispatcher re-reads coordinator state and rejects stale incidents.
5. The coordinator prepares a new monotonically increasing deployment epoch.
   The old deployment remains authoritative.
6. OCI first attempts `VM.Standard.A1.Flex` with the configured free-tier-sized
   OCPU/RAM allocation in the **same availability domain as the durable block
   volume**.
7. Only an explicit host-capacity failure is eligible for paid fallback.
   Authentication, IAM, image, subnet and other errors fail recovery rather than
   being misclassified as a capacity problem.
8. If paid fallback is enabled, the dispatcher verifies:
   - global external-compute switch is enabled,
   - no existing paid fallback exceeds the concurrency limit,
   - TTL is within the configured hard range,
   - configured hourly estimate is positive,
   - `hourly estimate × TTL <= per-incident budget`.
9. The replacement VM is launched before the durable volume is detached.
10. After launch succeeds, the dispatcher stops the old attached instance,
    detaches the volume, and attaches it paravirtualized to the candidate.
11. Cloud-init:
    - installs the minimal recovery runtime,
    - waits for the durable filesystem UUID,
    - mounts `/srv/katcha`,
    - restores the production env and AWS bundle from OCI Vault using instance
      principal authentication,
    - overrides release SHA, deployment ID and epoch,
    - checks out the exact release,
    - starts the production systemd service,
    - waits for local API readiness,
    - proves the candidate is still fenced,
    - waits for the Cloudflare Tunnel public health route,
    - reports all readiness checks to the Durable Object.
12. The dispatcher waits for that readiness receipt.
13. Only then does it commit the new epoch. The previous host becomes stale and
    fails every fenced external mutation.
14. If candidate acceptance fails, Katcha attempts to move the volume back,
    restart the previous instance, abort the pending epoch and terminate the
    failed candidate.

## Paid fallback expiry

Paid fallback instances carry these OCI free-form tags:

- `KatchaRecoveryMode=paid-fallback`
- `KatchaRecoveryIncident=<incident UUID>`
- `KatchaDeploymentId=<deployment identity>`
- `KatchaDeploymentEpoch=<epoch>`
- `KatchaExpiresAt=<UTC timestamp>`

The recovery workflow runs a cleanup pass every 15 minutes. Once the expiry
timestamp is reached it soft-stops and terminates the paid instance. Cleanup runs
even when the global external-compute kill switch has subsequently been turned
off, so disabling new spend cannot accidentally disable cleanup of existing
spend.

A later return-to-free migration slice should normally replace the paid leader
with a new A1 candidate before expiry. Expiry remains a hard last-resort spend
ceiling rather than an automatic extension.

## Same-AD and cross-AD recovery

The dispatcher first attempts same-AD recovery and reattaches the existing durable volume when that AD remains usable.

If same-AD free A1 recovery cannot proceed, the dispatcher tries configured alternate-AD free A1 targets before paid compute. Cross-AD recovery creates fresh durable storage in the target AD and restores the newest acceptable immutable R2 PostgreSQL/Temporal recovery point. The old OCI block volume is never treated as cross-AD attachable.

Only after configured free placements fail may the bounded paid fallback path run, and paid launch still requires durable external-compute budget authorization.

## GitHub configuration contract

The recovery workflow requires these repository secrets:

- `OCI_TENANCY_OCID`
- `OCI_USER_OCID`
- `OCI_FINGERPRINT`
- `OCI_API_PRIVATE_KEY`
- `KATCHA_RECOVERY_ADMIN_TOKEN`
- `KATCHA_RECOVERY_CANDIDATE_TOKEN`

And these repository variables:

- `OCI_REGION`
- `KATCHA_EXTERNAL_COMPUTE_ENABLED`
- `KATCHA_RECOVERY_COORDINATOR_URL`
- `KATCHA_PUBLIC_HEALTH_URL`
- `KATCHA_OCI_AVAILABILITY_DOMAIN`
- `KATCHA_OCI_COMPARTMENT_ID`
- `KATCHA_OCI_SUBNET_ID`
- `KATCHA_OCI_DATA_VOLUME_ID`
- `KATCHA_OCI_DATA_VOLUME_FS_UUID`
- `KATCHA_OCI_PRODUCTION_ENV_SECRET_ID`
- `KATCHA_OCI_AWS_BUNDLE_SECRET_ID`
- `KATCHA_OCI_ASSIGN_PUBLIC_IP`
- `KATCHA_OCI_PRIMARY_SHAPE`
- `KATCHA_OCI_PRIMARY_IMAGE_ID`
- `KATCHA_OCI_PRIMARY_OCPUS`
- `KATCHA_OCI_PRIMARY_MEMORY_GB`
- `KATCHA_OCI_PAID_FALLBACK_ENABLED`
- `KATCHA_OCI_PAID_FALLBACK_SHAPE`
- `KATCHA_OCI_PAID_FALLBACK_IMAGE_ID`
- `KATCHA_OCI_PAID_FALLBACK_OCPUS`
- `KATCHA_OCI_PAID_FALLBACK_MEMORY_GB`
- `KATCHA_OCI_PAID_FALLBACK_TTL_HOURS`
- `KATCHA_OCI_PAID_FALLBACK_ESTIMATED_HOURLY_USD`
- `KATCHA_OCI_PAID_FALLBACK_MAX_INCIDENT_USD`
- `KATCHA_RECOVERY_CANDIDATE_TIMEOUT_SECONDS`

No secret should be embedded in the repository.

## Cloudflare coordinator configuration contract

The recovery Worker requires these secrets:

- `FENCE_TOKEN`
- `RECOVERY_ADMIN_TOKEN`
- `RECOVERY_CANDIDATE_TOKEN`
- `RECOVERY_DISPATCH_TOKEN`

The checked-in Worker configuration sends recovery incidents to this repository's
GitHub `repository_dispatch` endpoint. The dispatch token should be scoped only
to this repository and only to the permission required for repository dispatch.

## OCI instance-principal contract

Recovery cloud-init uses `--auth instance_principal` for Vault. The recovery
instance therefore needs an OCI dynamic-group/IAM policy that can read only the
two bootstrap secrets it requires:

- production Katcha environment bundle
- hosted AWS authentication bundle

It should not receive broad Vault administration privileges.

## Current cutover boundary

The recovery automation described above is implemented and covered by repository tests.

Remaining work is account-side provisioning and real acceptance:

1. create the initial OCI network/subnets/A1 instance/block volume/Vault/dynamic-group resources,
2. configure the Cloudflare recovery Worker, Tunnel and edge policies,
3. create/scoped R2 media, backup and break-glass storage,
4. install real GitHub variables/secrets,
5. run same-AD, cross-AD, paid-fallback, break-glass, stale-host and local-PC-off acceptance against live infrastructure.

Use `docs/LIVE_CLOUD_CUTOVER.md` as the authoritative ordered runbook.
