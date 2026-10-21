# Host-initiated Katcha release observation

**Status: read-only candidate detection implemented; production promotion not enabled.**
This is a separate stage toward automatic cloud deployment, not a deployer.

## Behavior and trust boundary

The OCI production service runs `systemd katcha.service` with an **attached**
`docker compose up`. A competing Compose invocation can interrupt current
workers and Temporal histories; a successful GHCR image build by itself cannot
authorize promotion.

The observer performs only anonymous GitHub HTTPS reads and reads configured
`KATCHA_RELEASE_SHA` and optional `KATCHA_API_RELEASE_SHA` from
`/etc/katcha/katcha.env`. It does not disclose credentials, launch a Bastion
tunnel, use an OCI admin session, create compute, call Docker, touch R2, modify
a config file, install migrations or restart a service.

The candidate must be accepted `main`, supported by successful exact-head CI
and **later** successful runtime image publication, and descend from the
currently configured global SHA. Compare evidence must be complete and bounded;
300-file results, more than 250 commits, non-descendants and malformed evidence
fail closed. The output identifies migration, worker and infrastructure changes,
remaining commit/file headroom, and `cadence_review_due: true` when drift
reaches 75 commits or 100 changed files. These are early review thresholds,
not deployment permissions. A second `main` lookup detects an update during
observation.

Output is a single JSON line, with states `CURRENT`, `REVIEW_REQUIRED`,
or `BLOCKED`; `BLOCKED` exits 2. These are **configuration observations**,
not attestations of the running Docker images or Cloudflare public route.
`REVIEW_REQUIRED` is explicitly **not** approval to deploy.

## Validation and adoption

From WSL, source tests are non-destructive:

```bash
cd ~/src/katcha
python -m pytest -q tests/test_production_release_observer.py
```

Do not enable this on the old OCI checkout until its exact code/version and
available Python runtime have been inspected. The observer depends on the
latest accepted source in `/opt/katcha`; it does not update that source.

When a separately reviewed, safe production checkout update provides these
files, an operator may explicitly install **only** the observer units on the
verified OCI host:

```bash
cd /opt/katcha
sudo bash deploy/scripts/install-release-observer.sh --install
sudo systemctl start katcha-release-observer.service
sudo journalctl -u katcha-release-observer.service -n 20 --no-pager
```

`--install` does not enable the timer, alter `katcha.service`, update the
release, touch Cloudflare, or change recovery switches. After separate
acceptance of read-only scheduled operation, the operator can run:

```bash
cd /opt/katcha
sudo bash deploy/scripts/install-release-observer.sh --enable
sudo systemctl list-timers katcha-release-observer.timer
```

The hourly timer is **not enabled by the existing production-unit installer**.
To disable the observer only:

```bash
sudo systemctl disable --now katcha-release-observer.timer
```

## Separate production deployment controller

A protected, signed host-pull production deployer is implemented, but remains
**disabled by default**, independently of this release observer. The reviewed
deployment gate, bounded release comparator, exact migration policies, backup
and restore, Temporal inventory and full-stack supervisor-safe restart live in
`deploy/scripts/production-deploy.py`. See
[PRODUCTION_DEPLOYMENT_RUNBOOK.md](PRODUCTION_DEPLOYMENT_RUNBOOK.md).

Enabling the **read-only observer** does not enable production deployment.
The running checkout must include the observer files before installing them;
do not manually replace the running source just to start monitoring. After
the next approved release, consider enabling the observer hourly and reviewing
any `cadence_review_due` status before the delta approaches 250 commits or
300 files. A journald observation is **not** a notification unless an external
alerting path is separately connected.

Never reuse OCI recovery, cleanup or paid-compute enablement as routine deploy
authorization. A release that includes incompatible migrations or Temporal
changes must remain supervised; old images must not be restarted blindly
against a newer schema. No production mutation is performed by this PR.
