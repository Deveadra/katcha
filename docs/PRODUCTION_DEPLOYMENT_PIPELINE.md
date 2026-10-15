# Katcha production deployment pipeline

Status: **stage 1 implemented; production promotion NOT enabled**.
This document describes release publication separately from applying a release to
the active OCI production primary.

## Current source of truth

- Authoritative code: reviewed pull requests merged into `main`.
- CI: `.github/workflows/ci.yml`. A successful **push-to-main** CI run
  is mandatory before an image may be published by
  `.github/workflows/runtime-images.yml`.
- Image registry: `ghcr.io/deveadra/`. Hosted images are pinned to the
  **40-character Git commit SHA** in `KATCHA_RELEASE_SHA`; the mutable
  `:main` tags must never be production deployment inputs.
- Active production host: OCI A1, systemd `katcha.service`, and
  `deploy/docker-compose.production.yml`, accessed externally via Cloudflare.
- Release on host: `/etc/katcha/katcha.env` supplies
  `KATCHA_RELEASE_SHA`. Publishing new GHCR images does **not** modify this
  file or restart the host.

## Stage 1: CI-gated image publication (this PR)

1. PR checks build only affected images without pushing.
2. CI on accepted `main` completes **successfully**.
3. The image workflow triggers from the completed CI event. It rejects PR CI,
   scheduled CI, cancelled/failed CI, foreign repositories and stale commits
   that have already been superseded on `main`.
4. It checks out the exact CI-tested SHA, builds multi-architecture images,
   publishes SHA and compatibility `:main` tags, and verifies anonymous pulls
   and OCI image revision labels for all six images.
5. Any failure keeps the SHA **not publish-accepted**; no production mutation is
   performed. Existing images may remain in GHCR. A successful image workflow
   is not evidence that production was upgraded.

## Optional API-only pin (supervised; not automatic promotion)

`KATCHA_API_RELEASE_SHA` is an optional **exact immutable commit SHA** for only
the `api` Compose service. When absent, `api` uses the global
`KATCHA_RELEASE_SHA` like all other services. When set, the API image tag
and the container's `KATCHA_RELEASE_SHA` environment value are both the API
SHA, ensuring `/v1/health/release` attests the actual served release.
Worker, scheduler, renderer, migration, PostgreSQL and Temporal services retain
the global pin. Because the same production env file is injected into several
runtime containers, Compose explicitly masks `KATCHA_API_RELEASE_SHA` to an
empty value in every non-API env-file consumer. Setting the API pin therefore
cannot change those service configurations or trigger worker recreation.
This permits a narrowly scoped hosted UI/API hotfix without migrating the
database or restarting the existing workflow workers.

**This change only enables service selection; it does not deploy anything.**
The production operator must independently verify accepted release CI, published
arm64 image digest/revision, unchanged database requirements, fresh backups,
Temporal inventory, correct existing leadership, and a clean production checkout
before making an explicit API-only change. The host must run this Compose source
revision to recognize the new variable. Save the old environment and image
identity for rollback; pin both SHAs, never `:main`. Using a separate API SHA
does not bypass change review and is not authorization for autonomous rollout.

The existing systemd supervisor owns an attached `docker compose up` process.
A separate `docker compose up -d --no-deps api` may affect that attached
process. **Do not improvise a concurrent Compose promotion or restart systemd**
without verifying/controlling supervisor behavior, lock ownership, and rollback.
A full host restart will naturally restart workers (not duplicate them), with
each service resolving its appropriate stored immutable release pin.
The routine autonomous promotion stage below remains unimplemented.

## Stage 2: verified production promotion (NOT implemented)

Before automatic live deployment, implement **and independently review**:

- A candidate release record bound to the exact successful CI workflow run,
  the successful image workflow run and independently checked immutable image
  digests. Re-check current `main` and permissions immediately before publish.
- A production deployment mechanism that does **not** require an operator's
  interactive OCI login or temporary Bastion tunnel (e.g. a host-initiated,
  mutually authenticated release pull). It must have a dedicated
  `KATCHA_PRODUCTION_DEPLOY_ENABLED` gate defaulting to false. **Do not**
  reuse recovery or paid-compute switches as release-deployment authorization.
- A strongly serialized operator/agent rollout lease coordinated with the
  existing active production identity and fencing epoch. Never invoke OCI
  disaster recovery, leader handover, new compute, or cleanup for a routine
  application update.
- Preflight current running SHA and exact image digests, volume and PostgreSQL
  backups, migration compatibility, Temporal workflow/worker safety, public
  health and queue idempotence. Prevent production workers from running in
  duplicate; allow additive and backward-compatible DB migrations only in
  unattended promotions. Reject unknown/downgrade/irreversible transitions.
- Staged pull before change; bounded drain, controlled restart, database/API/
  worker readiness, Cloudflare public probe, deployed SHA attestation and
  durable deployment event history.
- Explicit failure states and a non-destructive rollback strategy tested with
  durable state and migrations. Never blindly return to an old image against
  a newer incompatible schema; never discard databases or R2 media.
- Operator notifications and a clear opt-out/maintenance control. Configure
  secret access and GitHub deployment approvals without putting administrator
  credentials on the CI runner.

## Operator verification today

From an unprivileged browser or terminal, check
`https://app.katcha.stream/v1/health/ready`. HTTP readiness alone is **not**
proof of the deployed Git SHA. A privileged *read-only* inspection of the OCI
host environment (without printing secrets) is required to establish its
current `KATCHA_RELEASE_SHA`; use the checked-in operator access runbook
`docs/OCI_OPERATOR_ACCESS.md` for accessing the host if needed.

The Katcha CLI report
`vault_admin_session: NEEDS_BROWSER_LOGIN` and
`bastion_tunnel: STALE_MANAGED` does not determine the public site's
availability. Routine production browsing must not depend on either.

## Hard stop

Do not interpret this stage-1 PR, a merged `main`, or a successful GHCR
publication as authorization to deploy or mutate production. Keep OCI
recovery, paid fallback and cleanup disabled until separately accepted.
