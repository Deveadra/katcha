# OCI production rollout preflight — running-container attestation

**Status: implemented as a read-only preflight; no production rollout executor.**

The GHCR publisher and release observer cannot establish what is actually
running on OCI. A merged commit and an image build do not update the active
release pin or replace Docker containers.

## Verified locally without mutation

The preflight checks the active systemd owner; all nine running Compose services;
image/tag and revision-label identity; Linux ARM64 images and locally recorded
GHCR registry digest references; and local-only `/v1/health/ready` and
`/v1/health/release` via `127.0.0.1:8000`.

Missing, duplicate, unexpected or unhealthy containers, invalid pins, missing
required healthchecks, bad architecture, absent digest references or a mismatched
API SHA return a `BLOCKED` JSON result and exit 2. It reads full Docker
metadata only in-process, never returning raw environment variables or errors.
Commands use an explicit non-mutating allowlist with no shell execution.

The separately pinned `KATCHA_API_RELEASE_SHA` is evaluated only for the API;
other workers must match global `KATCHA_RELEASE_SHA`. The report is **not**
an OCI leader/fence attestation, remote-digest verification, a Cloudflare
acceptance check, an R2 backup check or a migration/Temporal safety approval.

## Operator usage after the host checkout is independently verified

```bash
cd /opt/katcha
sudo PYTHONPATH=/opt/katcha/src python3 \
  -m katcha.ops.production_rollout_preflight \
  --production-env /etc/katcha/katcha.env
```

To also verify an accepted candidate against CI, published images, and
the exact current main revision (no deployment is performed):

```bash
cd /opt/katcha
sudo PYTHONPATH=/opt/katcha/src python3 \
  -m katcha.ops.production_rollout_preflight \
  --production-env /etc/katcha/katcha.env \
  --candidate-sha EXACT_40_CHARACTER_SHA
```

Candidate mode rejects mixed API/global pins until supervised reconciliation.
These commands do not enable an observer timer or update production. Do not
manually replace the live checkout just to acquire this preflight script.

## Outstanding gates before an actual autonomous deployment

1. Independent verified image digests, a dedicated disabled-by-default
   deployment permission gate, and serialized exclusive host lock.
2. OCI coordinator authority/epoch and no pending fence; fail closed on
   coordinator unavailability.
3. Fresh restorable R2/PostgreSQL backup, migration compatibility and running
   Temporal workflow compatibility.
4. Supervisor-owned bounded drain/stop/start with no competing attached
   Compose process and no duplicate workers.
5. Post-deploy public routes, operator authentication, media/workspace/worker
   checks, a durable result and schema-safe rollback.

Nothing in this PR calls a production write operation, changes a release
configuration, or toggles OCI recovery/external compute/paid flags.
