# Katcha production Compose owner lease

Status: **implemented in source; no OCI adoption or rollout activation performed**.

The existing `katcha.service` uses a long-lived, attached
`docker compose up --remove-orphans` process. A second invocation can
collide with it. The production supervisor now obtains an exclusive,
nonblocking Linux `flock` on FD 9 before configuration checks, pulls or
Compose execution, then preserves FD 9 through its final `exec`.

The lock resides at
`/run/katcha-production-supervisor/compose.lock` in a private root-owned
0700 directory with a root-owned 0600 file. Symlinks, mismatched modes,
and a busy lock fail closed. The kernel releases the lock when the
attached process exits; no stale PID-file cleanup is necessary.

**Critical adoption distinction:** This guard does not protect the already
running OCI process until the host adopts the new supervisor source through
a separately reviewed controlled service lifecycle. Merely merging to GitHub
or copying the file cannot make the currently attached Compose process
hold the lease. Never launch a second Compose command assuming this lease
protects an older uninstrumented supervisor.

For reviewers, the change neither changes the systemd unit nor alters
the Compose services, runtime SHA, R2 media, PostgreSQL, Temporal, or
OCI recovery flags. Unit installation and supervised host adoption
remain separate.

A future rollout controller must **share this lease protocol** and
must not run Compose concurrently with an attached owner. An actual
rollout additionally needs coordinator epoch/fence checks, independent
image provenance, fresh verified backups, migration compatibility,
worker drain/recovery, immutable release pins, durable audit receipts,
public health validation, and an explicit disabled-by-default
`KATCHA_PRODUCTION_DEPLOY_ENABLED` gate. This lease is necessary but
**not sufficient** authorization for autonomous deployment.

Verification on a development checkout:

```bash
cd ~/src/katcha
python -m pytest -q tests/test_production_supervisor_lease.py
bash -n deploy/scripts/production-supervisor.sh
```
