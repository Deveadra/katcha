# Proactive operations health

Operations monitoring runs independently of release approval and cutover. The
existing Operations workspace has an **Operations Health** panel for a global
operator: current checks, outstanding decisions, bot actions, notification state
and recent history. Channel-scoped principals cannot read host incidents.

## Continuous checks

The separate root-owned `katcha-operations-monitor.timer` runs every five minutes,
starting 90 seconds after boot. Normal production-unit setup includes the timer.
Its frozen installation can begin reporting without restarting the application.
Release comparisons are cached for 15 minutes to limit remote API traffic. Exact
release diagnostics refresh hourly; unchanged reviews do not repeat alerts.

| Signal | Evidence and attention policy |
| --- | --- |
| Backups | Hourly timer, latest service result, positive journal completion after protected R2 upload; overdue after two hours. Skipped jobs are not success. |
| Restore tests | Weekly timer, latest service result, positive isolated three-database restore completion; overdue after eight days. Partial restores are not accepted. |
| Background workflows | Bounded failed-execution inventory, safe workflow-type counts and **read-only terminal-history classifications** for up to three recent failed runs; also classifies repeated pending-activity errors. Emits codes such as `DISK_FULL`, `RATE_LIMIT`, `AUTHENTICATION`, `NETWORK`, `TIMEOUT`, `DEPENDENCY_MISSING`, `CODE_COMPATIBILITY` and a targeted follow-up. Never sends raw exceptions or IDs. A failed-run falling out of the one-hour query does not prove recovery; retain the incident through six continuously monitored quiet hours. A gap over 15 minutes resets the clock. |
| Release cadence | Existing observer's bounded descendant comparison, even while main's CI is incomplete; warnings at 60 commits or 80 files, ahead of the 75/100 review thresholds and 250/299 hard limits. |
| Next release | Exact candidate and migration presence. The bot invokes the existing deployer's read-only `--diagnose-release` and records review counts ahead of approval. |
| Authorization and recovery | Existing deployment-control diagnostics and running API authority verification. An open host gate or enabled automatic deployment polling raises attention. Signed requests retain the existing protected approval/notifier path. |
| Infrastructure | Required production containers and API readiness. Independently check root (`/`) and production data (`/srv/katcha`) for free bytes (<20% warning, <10% critical) and free inodes (<15% warning, <5% critical), with unknown rather than healthy when a probe fails. Read bounded Docker Compose container OOM, stopped-state and cumulative restart metadata without exposing Docker configuration/environment. Historical restart counts are informational, not automatically incident-worthy. |
| Notifications | Existing protected Telegram configuration and durable delivery state. No second update consumer is started. |

Missing evidence is unknown, never healthy. Journal retention must cover the
eight-day restore window. Missing historical completion logs require a fresh
safety job. These observations do not replace backup integrity verification,
disaster failover rehearsals, Temporal replay or signed release approval.

## Diagnose a recurring Temporal alert

The operations notification now includes a count of recent failed executions,
a sanitized workflow-type summary, **diagnostic cause categories**, and specific
next actions. The monitor reads the terminal Temporal event for up to three
failed runs per observation and recognizes last-failure categories for pending
activities after three attempts. It never forwards raw exception messages, stack
traces, task inputs, workflow IDs, authentication material or full event history
to Telegram or the public snapshot. `HISTORY_UNAVAILABLE` and `UNCLASSIFIED`
mean there is insufficient evidence to determine the cause, not a successful run.
The classifications are evidence-derived hints, not proof of an external dependency
being repaired. To inspect all affected executions and full detailed errors on
the primary, use these read-only commands. They neither rerun nor terminate work:

```bash
# On katcha-prod-primary-001, inspect failures from the preceding 24 hours.
CUTOFF="$(date -u -d '24 hours ago' +%Y-%m-%dT%H:%M:%S+00:00)"
sudo docker exec katcha-production-temporal-1 temporal --output json workflow list \
  --address temporal:7233 --namespace default \
  --query "ExecutionStatus=\"Failed\" AND CloseTime > \"$CUTOFF\"" \
  --limit 101 | python3 -c '
import json, sys
rows = json.load(sys.stdin)
if not isinstance(rows, list):
    raise SystemExit("Temporal failed-run inventory is not a list")
if len(rows) > 100:
    raise SystemExit("Temporal inventory may be truncated; use narrower date windows")
for row in rows:
    execution = row.get("execution") or {}
    kind = row.get("type") or row.get("workflowType") or {}
    print("type:", kind.get("name", "unknown"),
          "workflow_id:", execution.get("workflowId"),
          "run_id:", execution.get("runId"),
          "closed_at:", row.get("closeTime", "unknown"))
'
```

For any returned execution, retrieve its **actual failure event** with the
existing Temporal CLI and correlate the same time window with
`sudo docker logs --since 24h --tail 500 katcha-production-worker-1`,
`katcha-production-intelligence-worker-1`, and other applicable registered
workers. An empty failed list may mean a time-window mismatch or purged history,
not a repaired dependency. Do not paste raw history, full exceptions, worker
environment, or credential-bearing logs into public issues. Redact URLs and
tokens before sharing diagnostic excerpts. Use the workflow ID/run ID from the
listing; do not guess or manually retry potentially non-idempotent jobs.

Stalled-work detection is intentionally conservative: an activity still
pending **at least four hours** after its Temporal `scheduledTime` raises an
attention warning with count and oldest age. This is not a claim of failure:
long-running media processing can be legitimate. The monitor does not call a
long-lived scheduler stalled simply because its workflow start time is old.
Unknown/malformed activity timestamps are not converted to a guessed age.
Inspect the relevant activity's schedule-to-close policy, last heartbeat,
task queue worker polling and downstream media progress before considering a
retry or cancellation.

Newly diagnosed Temporal failure categories are fingerprinted from **safe
workflow type names and allowlisted cause codes**, not raw execution IDs, error
strings or volatile failure counts. An active incident raises a new notification
only if a different category combination persists for two consecutive monitor
observations. This avoids an alert for every five-minute count change while
still notifying for a materially changed failure pattern. The fingerprint is
a diagnostic category hash, not a unique execution identity; individual
failure attribution and private evidence review require a separate workflow
evidence inspector.

The monitor shows classified cause summaries, not full error messages. A six-hour quiet close means that
no new failure was observed under continuous monitoring; it is not proof that
earlier failed work reran, resumed, or reached its business outcome.

## Host pressure first-response

The root filesystem and data volume are separate health signals. A healthy
`/srv/katcha` volume does not imply the root filesystem has room for
Docker layers or service logs. Both byte and inode capacity are checked.

For an alert, inspect read-only evidence before any cleanup:

```bash
df -h / /srv/katcha
df -i / /srv/katcha
sudo docker system df
sudo docker ps -a --filter label=com.docker.compose.project=katcha-production
```

Docker metadata can identify OOM-killed and stopped containers, plus their
**cumulative** restart counts. A nonzero restart count alone does not prove
recent instability. Do not run `docker system prune`, delete images or logs,
or remove volumes as automatic remediation. Check Docker storage growth and
the specific container journal first, and preserve release/backup evidence.

## Root-private Temporal execution evidence

Each observation records at most 100 failed execution identities seen by Temporal
in the root-only `ledger.sqlite3`. At most three recent execution histories per
tick are inspected for a cause classification; others are explicitly
`HISTORY_NOT_SAMPLED`. Records are keyed idempotently by a digest of workflow
ID and run ID, with first and last observed timestamps. This preserves the
identity of failed work after it leaves the one-hour monitoring window. Records
older than 14 days are pruned, with a hard maximum of 2,000 retained rows.
This affects only the diagnostic ledger, **not** Temporal histories, media,
job records or backups.

The evidence table stores exact workflow/run IDs **only** in the root-private
ledger (file mode 0600); no raw exceptions, task payloads or histories are
stored there. The existing public API and Telegram snapshot never mount or
serialize this evidence table. The new read-only inspector defaults to hashed
lookup IDs and category codes:

```bash
# On the primary, after review/merge of the exact accepted source:
# Read only from the accepted main branch. This does not deploy the application.
sudo git -C /opt/katcha fetch origin main
sudo git -C /opt/katcha show origin/main:scripts/inspect-operations-temporal-evidence.py \
  | sudo python3 - --limit 20

# Only for a private operator investigation requiring exact Temporal IDs:
sudo git -C /opt/katcha show origin/main:scripts/inspect-operations-temporal-evidence.py \
  | sudo python3 - --limit 20 --private-ids
```

Telegram notification events with a numbered incident now include an
`/operations?incident=<id>#operations-health` link. Activate the reviewed
incident-link UI from PR #543 before relying on this destination for direct
inspection. It shows only the authenticated, sanitized and bounded snapshot,
not the private execution evidence.

Do not post `--private-ids` output publicly. Without a deployment/monitor
installation and live CLI acceptance, the existence of this source code is not
proof production evidence is being collected. A successful workflow retry
still requires separate business-output verification; this table only
preserves failures and diagnostic categories.

## Bounded remediation bot

New incidents enter a persistent queue immediately. The bot prepares diagnostics
and release review. It may request **one** existing backup or isolated restore
job per incident, with a one-hour cooldown between incidents of the same kind.
Both jobs must be idle, the timer enabled, production services/storage healthy,
and current runtime authority verified. It uses the root deployer's existing lock.

The bot commits its claim before calling systemd. An uncertain start is never
redispatched automatically. Recovery needs a later positive completion newer than
the attempt. A finished retry without new proof requires prompt operator review;
an unverified running backup after 50 minutes, or restore after 100 minutes, also
escalates. Normal schedules keep running.

The bot does not restart production, cancel workflows, change OCI/IAM, rotate
credentials, clear gates, approve migrations, delete data or call paid AI/providers.
Those incidents retain a specific operator decision. `--observe-only` skips safety
job starts, but still records observations and delivers configured alerts.

## Reporting and external watchdog

The private SQLite ledger is `/srv/katcha/operations/ledger.sqlite3` (0600).
Unresolved incidents deduplicate by check; severity changes, bot actions and
verified recovery append numbered events. Identical observations do not generate
repeated alerts. Failed Telegram sends remain queued for retry. Delivery is at
least once: a crash after acceptance and before the ledger commit may repeat a
numbered event. Only the configured existing operator chat/thread receives alerts.

An atomic allowlisted snapshot is written to
`/srv/katcha/operations/public/health.json`. Only this directory is mounted read
only into the API, which receives neither the ledger nor Docker/systemd access.
`GET /v1/operations/health` requires a global operator. Missing/invalid reports
are unavailable; reports older than 15 minutes are stale. Viewing the page never
launches host work. The UI shows the latest 100 history records; the private ledger
retains full history.

The Operations Health panel supports an authorized deep link
`/operations?incident=<numeric-id>#operations-health`. It displays only the
allowlisted summary and recent historical events from the existing snapshot,
with all text escaped; it does not grant API access or load private Temporal
history, Docker metadata, or the root ledger. If the incident is outside the
bounded recent snapshot the page reports this rather than claiming it is
resolved. The operator must still authenticate normally.

The **Operations heartbeat watchdog** GitHub workflow checks the minimal public
`/v1/health/operations` heartbeat every ten minutes. A dead host or stopped monitor
creates a durable GitHub incident and triggers the existing Telegram path. Delivery
failures are retried; confirmed recovery delivery closes the incident. GitHub
schedules can be delayed, so this is not hard real-time paging. The issue remains
the fallback when Telegram is unavailable. External outage history stays in GitHub
while Katcha is unreachable.

## Activation and evidence

After merge and passing required checks, extract and run the installer from
accepted main on the existing primary. It freezes the accepted monitor modules,
records their source SHA, enables the monitor and runs its first observation.
It does not advance the application checkout or perform a deployment.

```bash
sudo git -C /opt/katcha fetch origin main
set -o pipefail
sudo git -C /opt/katcha show origin/main:deploy/scripts/install-operations-monitor.sh | sudo bash -s -- --enable
sudo systemctl is-active katcha-operations-monitor.timer
sudo journalctl -u katcha-operations-monitor.service -n 20 --no-pager
sudo python3 -m json.tool /srv/katcha/operations/public/health.json
```

The installer also enables the existing deployment receipt/expired-request
notification timer, independently of deployment polling. The existing root
deployment tools must already be installed; refresh them with
`install-production-deployer.sh --install` if absent. Never print the production
environment or signing key. `--install` installs without enabling or running the
monitor. Disable only this monitor with:

```bash
sudo systemctl disable --now katcha-operations-monitor.timer
sudo systemctl stop katcha-operations-monitor.service
```

The dashboard and public heartbeat also require the reviewed API/Compose rollout
with the read-only snapshot mount. Before that rollout the new heartbeat is
unavailable and the external watchdog raises an activation incident. Verify the
timer, fresh snapshot, watchdog run and confirmed Telegram receipt. Merged source
does not prove that the host is monitoring or its secrets are configured.

Test failures and recovery with offline fixtures first. On production, observe
an actual scheduled completion and confirmed alert before claiming end-to-end
acceptance. Never break backups, permissions or workflows to test alerts.

Deployment retains its final fresh backup/restore, exact-SHA image, active workflow,
authority and migration checks. Monitoring moves investigation earlier.
