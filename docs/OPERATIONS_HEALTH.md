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
| Background workflows | Bounded inventory, pending activity attempts and recently failed executions. Running schedulers alone are healthy; repeated activity failures raise attention. |
| Release cadence | Existing observer's bounded descendant comparison, even while main's CI is incomplete; warnings at 60 commits or 80 files, ahead of the 75/100 review thresholds and 250/299 hard limits. |
| Next release | Exact candidate and migration presence. The bot invokes the existing deployer's read-only `--diagnose-release` and records review counts ahead of approval. |
| Authorization and recovery | Existing deployment-control diagnostics and running API authority verification. An open host gate or enabled automatic deployment polling raises attention. Signed requests retain the existing protected approval/notifier path. |
| Infrastructure | Required production containers, local API readiness and persistent storage; warn below 20% free, critical below 10%. |
| Notifications | Existing protected Telegram configuration and durable delivery state. No second update consumer is started. |

Missing evidence is unknown, never healthy. Journal retention must cover the
eight-day restore window. Missing historical completion logs require a fresh
safety job. These observations do not replace backup integrity verification,
disaster failover rehearsals, Temporal replay or signed release approval.

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
