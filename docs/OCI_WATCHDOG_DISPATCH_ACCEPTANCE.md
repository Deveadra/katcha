# OCI recovery watchdog dispatch and alert acceptance

## Safe activation boundary

Production OCI recovery remains disabled until approved live candidate and
failover acceptance has passed. `KATCHA_OCI_RECOVERY_CONFIGURED=false` and
`KATCHA_EXTERNAL_COMPUTE_ENABLED=false` are the required current settings.
No alarm or manual `repository_dispatch` event authorizes recovery while the
independent Python and GitHub Actions gates are disabled.

## Incident dispatch policy

The Cloudflare recovery Durable Object probes the current committed leader.
An incident is opened after the configured consecutive health failures.
The incident ID is reused for retries; GH Actions additionally serializes
recoveries in the `katcha-production-recovery` concurrency group and validates
the coordinator's current active epoch.

`max_dispatch_attempts` (1–10, default 3) caps **all** recovery dispatch
attempts for one unresolved incident, whether GitHub accepted the event or
returned an error. `recovery_retry_seconds` (300–86400) enforces the
minimum retry cooldown after the most recent submission attempt.
When the cap is exhausted the incident becomes `dispatch_exhausted` with
`last_dispatch_error` populated; it will not silently dispatch forever.
The state remains available for diagnosis. The configured health backstop
uses the same coordinator and never bypasses this state machine.

An incident with `dispatch_exhausted` requires an operator to check the
GitHub Actions run, coordinator epoch, primary health, recent backup/restore
markers, and possible ambiguous OCI resources. A new recovery attempt must
not be forced through a new incident or settings change until the existing
attempts are reconciled.

## Notification and validation

Failed recovery workflow runs execute the failure-only Telegram operator
step with a GitHub Actions run URL. Successful committed recovery uses the
existing success notification; neither indicates provider success unless
the corresponding readiness and authority checks pass. A missing notifier
credential or Telegram failure is itself a workflow failure, not proof
that an alert was delivered.

Offline validation:

```bash
node --test infra/cloudflare-recovery/test/*.test.mjs
python -m pytest -q tests/test_oci_recovery.py
```

Tests cover threshold, stale epoch, idempotent incident ID, cooldown,
bounded retries, candidate readiness and authoritative fencing. They are
simulations and **not** evidence of real GitHub-to-Cloudflare reachability,
live notifications, OCI resource creation, Vault or R2 restore.

## Live acceptance still required

After explicit authorization and read-only revalidation, prove one controlled
healthy check, one simulated false-health alert that cannot create OCI
resources, and independently verify Telegram delivery and coordinator
incident status. A real failover game-day is a separate disruptive approval.
