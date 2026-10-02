# Katcha Cloudflare recovery coordinator

This Worker is the external, strongly consistent authority used by hosted Katcha
to prevent split-brain mutations and to detect loss of the active control plane.

It is intentionally outside the OCI VM failure domain.

## What it owns

A single SQLite-backed Durable Object stores:

- current active deployment ID and monotonically increasing epoch
- one prepared replacement deployment
- watchdog configuration and health history
- one deduplicated recovery incident
- a bounded authority event trail

The active epoch is never reused.

## Authority handoff

A replacement is a two-phase handoff:

1. `POST /v1/authority/prepare` allocates the next epoch but does **not** fence
   the current leader.
2. Start the replacement with the returned deployment ID/epoch and restore or
   attach durable state.
3. Verify read-only health/reconciliation.
4. `POST /v1/authority/commit` atomically makes the prepared deployment active.
   The old deployment immediately fails Katcha's application-side fence.
5. Use `POST /v1/authority/abort` if the candidate cannot become healthy.

Both prepare and commit require `expected_active_epoch`; stale recovery attempts
receive HTTP 409 instead of overwriting newer authority.

## Endpoints

Public:

- `GET /healthz`

Fence token:

- `POST /v1/fence/assert`

Recovery-admin token:

- `GET /v1/authority/status`
- `POST /v1/authority/prepare`
- `POST /v1/authority/commit`
- `POST /v1/authority/abort`
- `POST /v1/watchdog/configure`
- `POST /v1/watchdog/probe-now`

The watchdog probes the committed deployment's exact HTTPS health URL. After the
configured consecutive-failure threshold it creates one incident and sends one
idempotent recovery dispatch. Failed dispatches are retried on later probes using
the same `Idempotency-Key`.

A recovered leader does not automatically receive a new epoch, and the watchdog
never commits a replacement. Recovery automation must re-check incident/authority
state before committing a candidate.

## Required secrets

Install with Wrangler; never commit values:

```bash
wrangler secret put FENCE_TOKEN
wrangler secret put RECOVERY_ADMIN_TOKEN
wrangler secret put RECOVERY_DISPATCH_TOKEN
```

`RECOVERY_DISPATCH_URL` is also required before watchdog recovery can be enabled.
It may be set as a secret or environment variable.

Use a different token for each trust boundary.

## Local validation

From the repository root:

```bash
node --test infra/cloudflare-recovery/test/*.test.mjs
npx --yes wrangler@4.146.0 deploy \
  --dry-run \
  --config infra/cloudflare-recovery/wrangler.jsonc
```

The repository CI runs both checks.

## Initial bootstrap

With no current leader, prepare with `expected_active_epoch: 0`. The returned
epoch will be 1. After the first control plane is healthy, commit it with
`expected_active_epoch: 0`.

## Recovery dispatcher contract

On watchdog failure the coordinator POSTs:

```json
{
  "incident_id": "uuid",
  "reason": "active_control_plane_health_threshold_exceeded",
  "observed_at": "ISO-8601 timestamp",
  "active_deployment": {
    "deployment_id": "oci-a1-primary",
    "epoch": 12,
    "health_url": "https://katcha.example/v1/health/ready"
  },
  "expected_active_epoch": 12,
  "failure_count": 3,
  "last_error": "health endpoint returned HTTP 503"
}
```

The request carries `Authorization: Bearer <RECOVERY_DISPATCH_TOKEN>` and
`Idempotency-Key: <incident_id>`.

The dispatcher is responsible for acquiring replacement compute, restoring
durable state, calling prepare, configuring the returned epoch on the candidate,
running recovery acceptance, and only then calling commit.
