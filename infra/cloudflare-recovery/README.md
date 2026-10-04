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
- a strongly consistent external-compute budget ledger

The active epoch is never reused.

## Authority handoff

A replacement is a two-phase handoff:

1. `POST /v1/authority/prepare` allocates the next epoch but does **not** fence
   the current leader.
2. Start the replacement with the returned deployment ID/epoch and restore or
   attach durable state.
3. The candidate proves local runtime health, durable-state attachment, fence
   reachability and the public Tunnel route, then calls
   `POST /v1/authority/candidate-ready`.
4. `POST /v1/authority/commit` refuses unready candidates and atomically makes
   a ready deployment active.
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
- `GET /v1/external-compute/status`
- `POST /v1/external-compute/configure`
- `POST /v1/authority/prepare`
- `POST /v1/authority/commit`
- `POST /v1/authority/abort`

Candidate token:

- `POST /v1/authority/candidate-ready`
- `POST /v1/watchdog/configure`
- `POST /v1/watchdog/probe-now`

External-compute token:

- `POST /v1/external-compute/reserve`
- `POST /v1/external-compute/settle`
- `POST /v1/external-compute/release`

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
wrangler secret put RECOVERY_CANDIDATE_TOKEN
wrangler secret put RECOVERY_DISPATCH_TOKEN
wrangler secret put EXTERNAL_COMPUTE_TOKEN
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


## GitHub recovery dispatch

The checked-in Worker configuration sends watchdog incidents to the GitHub
repository-dispatch endpoint using event type `katcha-recovery`. The
`RECOVERY_DISPATCH_TOKEN` should be a fine-grained GitHub token scoped only to
this repository with **Contents: write**, which is the permission GitHub requires
for repository-dispatch creation.

The resulting workflow is serialized with a single
`katcha-production-recovery` concurrency group, so concurrent watchdog/manual
recovery requests cannot provision two replacements.


## External-compute budget ledger

Paid infrastructure is disabled by default in the coordinator even if a caller
has cloud credentials. Enabling it requires an explicit recovery-admin
configuration.

All money values use integer **micro-USD** (1 USD = 1,000,000 micro-USD) so the
coordinator never uses floating-point money arithmetic.

Example: allow at most $10/month of external compute, with $5 assigned to OCI
emergency control-plane fallback and $5 assigned to Remotion Lambda rendering,
no more than two concurrent paid jobs, and at most $3 of attempts within one
retry group:

```bash
curl -fsS \
  -H "Authorization: Bearer $KATCHA_RECOVERY_ADMIN_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{
    "enabled": true,
    "monthly_limit_microusd": 10000000,
    "provider_limits_microusd": {
      "oci": 5000000,
      "aws-lambda": 5000000
    },
    "max_concurrent_jobs": 2,
    "max_retry_attempts": 3,
    "max_retry_spend_microusd": 3000000
  }' \
  "$KATCHA_RECOVERY_COORDINATOR_URL/v1/external-compute/configure"
```

A reservation succeeds only when all of these remain within bounds:

1. global external-compute switch is enabled,
2. monthly settled + reserved spend,
3. provider settled + reserved spend,
4. active reservation count,
5. retry-group attempt count,
6. retry-group settled + reserved spend.

Reservations are idempotent by `job_key`. A released or expired job key cannot
be reused. Callers may provide an explicit monotonic attempt number, or omit it
and let the Durable Object allocate the next retry attempt atomically. Settled
job keys also cannot launch again.

The accounting month is UTC. Monthly and per-provider settled spend are stored
as durable aggregates, so bounded audit/history presentation cannot erase spend
from a live month's ceiling. On month rollover, monthly/provider totals reset
while still-active reservations carry forward conservatively. Retry-group
settled spend **and attempt counters** survive the rollover so a failing logical
job cannot escape either retry circuit at midnight UTC.

The global kill switch is evaluated before idempotent reservation reuse. Turning
external compute off therefore blocks even a replay of an existing reservation
from authorizing another provider launch. The coordinator also refuses more than
5,000 reservation records in one UTC month and more than 10,000 durable retry
groups. These are fail-closed state-growth circuit breakers: Katcha never evicts
spend history merely to admit another paid job.

The OCI recovery runner reserves the **worst-case configured TTL cost before
launching any paid fallback instance**. OCI capacity failures that create no
instance release the reservation. Failures after a paid launch settle
conservatively, and TTL/leader retirement settles from the instance's tagged
runtime estimate.

The Remotion renderer reserves the configured per-render ceiling immediately
before `renderMediaOnLambda()`. The stable output identity is the retry group;
the individual launch gets a unique job key and a coordinator-assigned attempt.
Budget denial therefore happens before AWS invocation. Completed or uncertain
Lambda attempts settle conservatively at the reserved ceiling; a settlement
transport failure leaves the reservation held rather than failing a completed
render and causing duplicate paid work.

This ledger is intentionally outside PostgreSQL so budget enforcement remains
available when the Katcha VM or database is the component being recovered.
