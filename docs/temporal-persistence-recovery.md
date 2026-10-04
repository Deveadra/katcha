# Temporal database timeouts

When Temporal logs `GetTimerTasks` / `GetWorkflowExecution` SQL timeouts,
followed by `shard status unknown`, its persistence layer is unavailable or too
slow. Queue, rollback and activity-start errors can follow the same outage;
they do not establish that workflow history is corrupt.

The local stack now keeps Temporal's search-attribute cache enabled and limits
each Temporal service to five history and two visibility SQL connections, with
two and one idle connections respectively. Upstream defaults are 20 and 10 per
service. These smaller pools leave more room for Katcha's application workers on
the shared PostgreSQL instance. They mitigate connection pressure; they do not
prove that a particular outage was caused by exhaustion rather than CPU, memory,
disk latency or database locks.

After updating the repository, apply the changed container environment from the
repository root during a brief processing interruption:

```bash
cd ~/src/katcha
docker compose --project-name katcha up -d --no-deps temporal
docker compose --project-name katcha logs --since 5m --tail 100 postgres temporal
python scripts/diagnose_runtime.py
```

Use the same Compose project and configuration as the existing stack. The
standard Katcha launcher project is `katcha`. If you use a custom project name,
pass that same name with `docker compose -p NAME`.

`up -d` recreates Temporal when its environment changes. Existing PostgreSQL
volumes and workflow history remain in place; worker polls reconnect and durable
workflows can resume. Confirm jobs actually progress before declaring recovery.
The mounted dynamic configuration also reloads automatically, on Temporal's
60-second polling interval.

The diagnostic snapshot includes host/container resource usage and a bounded,
read-only PostgreSQL probe: total/max connections, active connections, idle
transactions, lock waiters and blocked connections. It excludes SQL text,
credentials and connection identities. The active count excludes the probe
itself; the total includes it. A probe timeout is reported separately, preserving
the container evidence.

If timeouts continue, collect that snapshot while the issue is happening.
Connections near the limit suggest contention; lock waiters suggest blocked
transactions; high CPU, low available memory, swap pressure or OOM evidence
suggest host resource pressure. A snapshot alone cannot rule out disk latency.
PostgreSQL and Temporal logs are still needed to distinguish these cases.

Do not remove volumes, reset workflows or change the history-shard count as a
timeout recovery step. Those operations can destroy data or invalidate an
existing cluster. Pool limits can be tuned using the Compose interpolation
variables `KATCHA_TEMPORAL_SQL_MAX_CONNS`, `KATCHA_TEMPORAL_SQL_MAX_IDLE_CONNS`,
`KATCHA_TEMPORAL_SQL_VIS_MAX_CONNS` and `KATCHA_TEMPORAL_SQL_VIS_MAX_IDLE_CONNS`;
keep idle limits at or below their corresponding maximums and account for
application connections before increasing them.

References: Temporal 1.27.2
[server configuration template](https://github.com/temporalio/temporal/blob/v1.27.2/docker/config_template.yaml)
and [dynamic settings](https://github.com/temporalio/temporal/blob/v1.27.2/common/dynamicconfig/constants.go).
