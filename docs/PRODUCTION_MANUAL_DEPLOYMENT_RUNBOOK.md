# Katcha: manual production deployment (repeatable operator checklist)

**Scope:** reviewed, CI-published release to the *existing* OCI A1 primary
(`katcha-prod-primary-001`). This is a **manual, one-request deployment**, not
continuous deployment. Do not enable the deployment timer. The only application
change is the **explicitly approved exact release SHA**. The existing signed
GitHub request, root-owned host deployer, strict schema/Temporal checks, and
backup/restore gates remain authoritative.

For initial Cloudflare/GitHub Environment and signing-key setup, use
[the full deployment runbook](PRODUCTION_DEPLOYMENT_RUNBOOK.md), Sections 2–3.
**Do not create a new signing key for a repeat deployment.** This checklist
assumes the Environment secret and the same root-owned OCI signing key already
exist. A successful GitHub request is **not** proof of production deployment.

## Which machine?

| Label | Shell prompt | Execute |
| --- | --- | --- |
| **WSL** | `tycarroll@Chronos:~/src/katcha$` | `gh`, local Git, Bastion connection, GitHub settings |
| **OCI** | `ubuntu@katcha-prod-primary-001:~$` | `sudo git -C /opt/katcha`, deployer, systemd, local health |
| **GitHub** | Web browser | Actions → Production deployment and protected Environment approval |

The commands in different shell sections are **not interchangeable**. Keep a
separate WSL terminal open while connected to OCI.

## 0. Choose a *single* approved release SHA (WSL + GitHub)

From **WSL**:

```bash
cd ~/src/katcha
git fetch origin main
git rev-parse origin/main
```

The output is only a **candidate**. Open
[GitHub Actions](https://github.com/Deveadra/katcha/actions) and identify a
commit that has (a) successful **push-to-main CI** and (b) successful
**Publish runtime images** for that **same exact 40-character SHA**, including
all seven published images. Do not use `:main` tags or a PR-only commit.
You may select an earlier immutable SHA still on accepted `main` ancestry if
newer merges have not finished image publication.

Save this SHA in your operator notes and use it **unchanged** on OCI and in
the GitHub workflow form. This runbook deliberately does not guess a SHA.

**STOP** if CI/images are missing, failed, or ambiguous.

## 1. Connect to OCI (WSL)

```bash
cd ~/src/katcha
bash scripts/katcha-oci-admin.sh status
bash scripts/katcha-oci-admin.sh connect
```

If status requires browser authentication, run
`bash scripts/katcha-oci-admin.sh reauth` in WSL and retry `connect`.
If Bastion reports a changed IP allowlist, follow
[OCI operator access](OCI_OPERATOR_ACCESS.md) rather than weakening SSH
host-key checking.

Then, still in **WSL**:

```bash
ssh -F /dev/null -4 \
  -i "$HOME/.ssh/katcha-oci" \
  -o IdentitiesOnly=yes -o StrictHostKeyChecking=yes \
  -o "UserKnownHostsFile=$HOME/.config/katcha/production/bastion/target_known_hosts" \
  -p 22022 ubuntu@127.0.0.1
```

**STOP** unless your new shell prompt identifies
`ubuntu@katcha-prod-primary-001`. Do not execute OCI commands on WSL.

## 2. Verify production and refresh *only* root-owned deployment tools (OCI)

These checks do **not** update production containers or database:

```bash
hostname
whoami
sudo git -C /opt/katcha rev-parse HEAD
sudo git -C /opt/katcha status --porcelain
sudo systemctl is-active katcha
sudo systemctl is-enabled katcha-production-deploy-poll.timer || true
sudo test -e /etc/katcha/production-deploy.enabled && \
  echo "STOP: deployment permission already present" || \
  echo "Host deployment permission absent"
sudo stat -c 'Key owner=%U mode=%a bytes=%s' \
  /etc/katcha/production-deploy.key
mountpoint /srv/katcha
```

Expect: correct hostname/user, clean Git status (no output), service
`active`, timer `disabled`, **permission file absent**, signing key owned
by root and mode `600` (65 bytes), and durable mount present. If any of
these differ, **STOP**; do not repair a dirty checkout with reset/rebase.

After the **current accepted-main CI** passed, update the reviewed root-owned
deployer using the checked-in installer *without* advancing `/opt/katcha`
or restarting any containers. Use the exact, isolated subshell:

```bash
(
  set -euo pipefail
  test "$(hostname)" = "katcha-prod-primary-001"
  test -z "$(sudo git -C /opt/katcha status --porcelain)"
  sudo git -C /opt/katcha fetch origin main
  TOOL_SHA="$(sudo git -C /opt/katcha rev-parse origin/main)"
  TEMP="$(mktemp --suffix=.sh)"
  trap 'rm -f "$TEMP"' EXIT
  sudo git -C /opt/katcha show \
    "$TOOL_SHA:deploy/scripts/install-production-deployer.sh" > "$TEMP"
  test -s "$TEMP"
  bash -n "$TEMP"
  sudo bash "$TEMP" --install
)
```

Expect `DEPLOYER_INSTALLED source_sha=... enablement=OFF runtime_untouched=true`.
`source_sha` identifies **reviewed deployment tools**, not necessarily the
application release you selected in Step 0.

Check the installer left deployment off:

```bash
sudo /usr/local/sbin/katcha-production-deploy-poll
sudo systemctl is-enabled katcha-production-deploy-poll.timer || true
sudo systemctl is-active katcha
```

Expected `{"status":"DISABLED"}`, `disabled`, `active`. **STOP** otherwise.

## 3. Perform exact-release read-only preflight (OCI)

Copy the selected full SHA from Step 0 into the first line. **Do not replace
it with a newly moved `main` without repeating the release checks.**

```bash
TARGET_SHA='PASTE_APPROVED_EXACT_40_CHARACTER_SHA'
[[ "$TARGET_SHA" =~ ^[0-9a-f]{40}$ ]] || {
  echo "STOP: use an exact approved 40-character lowercase SHA"
  false
}
sudo /usr/local/sbin/katcha-production-deploy --readiness
sudo /usr/local/sbin/katcha-production-deploy --plan-release "$TARGET_SHA"
```

If planning instead prints `host operation did not succeed: python3`,
the installed independent release verifier exited nonzero and the older host
deployer discarded its specific reason. This is **not** proof of a broken
Python installation. Inspect that installed, root-owned verifier directly:

```bash
sudo python3 /usr/local/lib/katcha-production-deploy/release_eligibility.py "$TARGET_SHA"
```

A `{"status":"BLOCKED","reason":"..."}` response identifies which CI,
image-job, GitHub evidence or release ancestry check failed. **Do not**
override a blocked result or infer deployment approval from workflow-level
success. Compare the individual exact-SHA CI and seven image jobs, plus
anonymous image-pull verification. If the installed verifier is outdated,
refresh deployment tooling only through Step 2's reviewed installer procedure;
do not update production application files or migration state to repair
diagnostics. A fixed deployer includes the exact sanitized verifier rejection
in `--plan-release` output.

Expected planner output:

- `status: REVIEW_REQUIRED`, **not** `BLOCKED`;
- `target_sha` is exactly the selected SHA, with
  `deployment_authorized: false`;
- starting database revision matches the inspected PostgreSQL catalog;
- `candidate_migration_policy` is compatible with the *actual* schema;
- `temporal_pending_activity_check: PASSED` and a reviewed active-workflow
  count;
- commit/file delta within limits (at most 250 commits and fewer than
  300 changed files).

For the **previously observed** `0065_acquisition_jobs` production database
and a release adding **only** `0066_acquisition_handoff.py`, the intended
candidate policy is `approve-additive-through-0066`. This **does not**
automatically approve applying the migration. A later/no-migration release
needs its own planner result. Do not manually stamp Alembic or use a broader
policy to bypass a `BLOCKED` result.

Six Temporal workflows were previously running. `approved-resume` requires
an explicit review of their identity, pending activity state, restart behavior
and idempotence; `refuse` is the default and will block a running-workflow
cutover. **STOP** until that decision is recorded and the release's worker
compatibility is understood.

Also verify recent R2 backups, the isolated three-database restore-test
evidence, leadership/fencing authority, host capacity, and a maintenance
window with a recovery operator. The executor will repeat fresh backups/
restore validation and re-inventory workflows **before** stopping production;
it aborts on changed state. An additive 0066 rollout is **forward-only**
after schema change, not a routine old-image rollback.

## 4. Authorize one signed release (WSL + GitHub browser)

On **WSL** (not OCI), after Step 3 and the release/Temporal review:

```bash
gh auth status
gh variable set KATCHA_PRODUCTION_DEPLOY_ENABLED \
  --repo Deveadra/katcha --body true
gh variable list --repo Deveadra/katcha
bash scripts/check-production-deployment-github.sh
```

The read-only preflight must print `GITHUB_DEPLOY_AUTH_SCOPE_OK`. It verifies:

- The **repository Actions variable**
  `KATCHA_PRODUCTION_DEPLOY_ENABLED` is `true`.
- No duplicate **`katcha-production` Environment variable** with that
  same name exists. An Environment-scoped duplicate can contradict the
  repository gate once the protected job starts. Inspect both scopes with
  `gh variable list --repo Deveadra/katcha` and
  `gh variable list --repo Deveadra/katcha --env katcha-production`.
  After independently confirming the duplicate, remove **only that
  Environment-level variable**, never the authoritative repository variable.
- The protected `katcha-production` Environment lists the
  `KATCHA_PRODUCTION_DEPLOY_HMAC_KEY` secret **by name**. Secret content
  cannot be retrieved by GitHub; name presence does not verify its format or
  equivalence to the root-only OCI key. Do not regenerate or print either key.

**This opens the GitHub request gate**, not the OCI deployment gate.
Keep the repository gate `true` until `authorize-request` succeeds and
logs `Production deployment request published`. Turning it off before
approval completes is a deliberate authorization denial. Do not interpret
`DEPLOY_KEY: ***` in the logs as proof that the HMAC matches the OCI key.

In **GitHub browser** open
[Actions → Production deployment](https://github.com/Deveadra/katcha/actions/workflows/production-deployment.yml),
choose **Run workflow**, and fill in:

| Input | Value |
| --- | --- |
| Workflow branch | `main` |
| `release_sha` | Exact SHA from Step 0 / 3 |
| `running_workflows` | `refuse`, unless reviewed `approved-resume` is expressly authorized |
| `migration_policy` | Exactly the reviewed planner policy, e.g. `approve-additive-through-0066` for verified 0065 → 0066 |
| `confirmation` | `DEPLOY KATCHA PRODUCTION` |

Approve the protected `katcha-production` Environment when GitHub prompts.
Verify `preflight` and `authorize-request` jobs **succeeded** and the
`Production deployment request published: id=... sha=...` message matches
the selected SHA. Approval creates a **20-minute signed request**, not a
deployed release. If authorization fails, read its distinct error:
`GITHUB_DEPLOY_GATE_DISABLED` points to variable scope/early disable;
`GITHUB_DEPLOY_SIGNING_SECRET_INVALID` points to absent/malformed secret
in the protected Environment. Neither is a Bastion/OCI SSH failure.
Do not loosen signing checks or expose credentials to debug either.

**Immediately close the GitHub request gate after that request is signed**
(WSL). The existing signed request remains valid until its original expiry:

```bash
gh variable set KATCHA_PRODUCTION_DEPLOY_ENABLED \
  --repo Deveadra/katcha --body false
gh variable list --repo Deveadra/katcha
```

Confirm `false` and proceed to OCI promptly. An expired request requires a
new approved dispatch, never relaxed validation. A future request requires a
fresh, explicit temporary opt-in.

## 5. Consume the request **once**, with no deployment timer (OCI)

Back in **OCI**. This is the **actual, intentionally mutating deployment
step**. Run it only after the above approval, backup/restore evidence,
migration review and Temporal policy decision are complete.

```bash
(
  set -euo pipefail
  test "$(hostname)" = "katcha-prod-primary-001"
  sudo systemctl disable --now katcha-production-deploy-poll.timer
  # Always remove the host opt-in after the one-shot command returns/errors.
  trap 'sudo rm -f /etc/katcha/production-deploy.enabled' EXIT
  printf '%s\n' 'KATCHA_PRODUCTION_DEPLOY_ENABLED=true' |
    sudo sh -c 'umask 077; cat > /etc/katcha/production-deploy.enabled'
  sudo stat -c 'Deploy gate owner=%U mode=%a' \
    /etc/katcha/production-deploy.enabled
  sudo /usr/local/sbin/katcha-production-deploy-poll
)
```

The fixed root-owned poller consumes at most **one** valid signed request.
It independently verifies authorization, expiry, exact SHA and policies;
the deployer then checks immutable images, schema, running workflows,
R2/restore, fencing and service ownership before any cutover.

The **manual poller console** reports `{"status":"SUCCEEDED", ...}` when
the fixed executor returns successfully. The separate root-private
`/srv/katcha/deployments/<timestamp>-<sha>/result.json` audit then records
`{"status":"DEPLOYED", ...}` for the exact SHA. Both must agree.
`NO_VALID_PENDING_REQUEST`, `BLOCKED`, `FAILED_REVIEW_REQUIRED`, or
`RECOVERY_REQUIRED` **are not successes**. The subshell removes the host
permission on normal exit and command failure; check it separately in Step 6.
Do not simply rerun a failed deployment request, restart Docker Compose,
modify the epoch, or downgrade a migrated database.

## 6. Verify release and shut off **both** gates (OCI, WSL, browser)

On **OCI**, with no change to the root service:

```bash
sudo systemctl is-active katcha
sudo git -C /opt/katcha rev-parse HEAD
curl -fsS http://127.0.0.1:8000/v1/health/ready
echo
curl -fsS http://127.0.0.1:8000/v1/health/release
echo
sudo systemctl disable --now katcha-production-deploy-poll.timer
sudo rm -f /etc/katcha/production-deploy.enabled
sudo systemctl is-enabled katcha-production-deploy-poll.timer || true
sudo test ! -e /etc/katcha/production-deploy.enabled && \
  echo 'Host deployment gate OFF'
sudo find /srv/katcha/deployments -type f -name result.json -print
```

The checkout SHA and local `/v1/health/release` response must match
**the exact approved target**. Check the latest protected result receipt for
`status: DEPLOYED`, postdeploy Temporal recovery counts, and the migration
revision. To inspect only the reviewed deployment outcome, substitute the
exact `result.json` path identified above:

```bash
sudo cat /srv/katcha/deployments/PASTE_EXACT_DEPLOYMENT_DIRECTORY/result.json
```

Do **not** use `cat` on signing keys, `production-env.before`, credentials,
or request payloads.

In the normal **authenticated** browser, verify
[app.katcha.stream](https://app.katcha.stream): Cloudflare Access login,
Channel Studio, sources, queue, review player, and channel-scoped evidence.
A bare public `curl` may receive an expected Cloudflare Access challenge;
do not weaken Cloudflare Access to make the probe pass.

On **WSL**, close the GitHub gate:

```bash
gh variable set KATCHA_PRODUCTION_DEPLOY_ENABLED \
  --repo Deveadra/katcha --body false
gh variable list --repo Deveadra/katcha
```

Confirm `false`, the OCI timer `disabled`, and the OCI permission file
absent. Archive the **approved target SHA, GitHub request ID, deployed receipt,
schema revision, restored-backup proof and validated Temporal run IDs**.

## 7. Failure or emergency stop (do not improvise a rollback)

To **stop new requests** on OCI without interrupting `katcha.service`:

```bash
sudo systemctl disable --now katcha-production-deploy-poll.timer
sudo rm -f /etc/katcha/production-deploy.enabled
sudo journalctl -u katcha-production-deploy-poll.service -n 60 --no-pager
```

Also set the **GitHub repository variable to `false`** from WSL as in
Step 6. When executed manually, the poller's primary result is in the
terminal (not necessarily the systemd journal). Inspect protected receipts
under `/srv/katcha/deployments` and the `katcha.service` journal without
disclosing tokens or credentials.

For a migration-bearing rollout that records `RECOVERY_REQUIRED`, keep the
durable PostgreSQL/Temporal/R2 state intact and use supervised forward repair.
**Never** reset/rebase the production checkout, `docker compose up`
concurrently with the attached supervisor, or restart old images against 0066.

## Optional: avoid release drift while keeping deployment manual

**Only after the new observer source is deployed on OCI** and read-only
observation is separately approved, install/enable its **independent** hourly
timer using [PRODUCTION_RELEASE_OBSERVER.md](PRODUCTION_RELEASE_OBSERVER.md).
It raises a review flag at **75 commits or 100 changed files** before the
strict 250-commit / 299-file comparison ceiling. Its journald output does
**not** deploy or send notifications by itself. The production **deployment**
timer stays disabled.


## Telegram notifications for review, completion, failure and expiry

This is a **separate notification path**, not a deployment trigger. It does not
alter GitHub Environment review, the signed request, the OCI deployment
authorization, or the deliberately manual release steps above.

### GitHub: prompt for protected review

The existing `scripts/notify-telegram.sh` sends the review link from an
unprotected `notify-review` job **only after** `preflight` passes and the
GitHub API reports the `katcha-production` Environment waiting for review.

In GitHub **repository Actions secrets**, configure the existing notification
settings `KATCHA_TELEGRAM_BOT_TOKEN` and `KATCHA_TELEGRAM_CHAT_ID`. The
optional repository variable `KATCHA_TELEGRAM_THREAD_ID` targets a Telegram
topic. The protected Environment's deployment **signing** secret remains
separate. Never move that signing secret to repository scope to send alerts.

Run the existing `telegram-notification-test.yml` manually to verify
the bot can deliver into your chat. The approval notification links to the
specific `https://github.com/Deveadra/katcha/actions/runs/<run-id>` page.
Open the link while authenticated to GitHub and use **Review deployments**.
The Telegram message itself cannot approve deployments. Very fast approvals
may occur before the notification job observes a pending review; it will not
mislabel such a run as waiting.

### OCI: independently notify from durable receipts

The installer in Step 2 also installs a **separate** root-owned
`katcha-production-deploy-notify.service` and
`katcha-production-deploy-notify.timer`. It does not enable them.
The notifier reuses `KATCHA_TELEGRAM_BOT_TOKEN` and
`KATCHA_TELEGRAM_CHAT_ID` from the existing **root-private**
`/etc/katcha/katcha.env`; optionally set
`KATCHA_TELEGRAM_THREAD_ID` there. It never prints secret values.

After merging this change, passing CI, and refreshing the host's
root-owned tools using Step 2, on the **OCI host**:

```bash
sudo systemctl start katcha-production-deploy-notify.service
sudo journalctl -u katcha-production-deploy-notify.service -n 20 --no-pager
sudo systemctl enable --now katcha-production-deploy-notify.timer
sudo systemctl is-enabled katcha-production-deploy-notify.timer
sudo systemctl is-enabled katcha-production-deploy-poll.timer
```

Expect the notification timer `enabled`, but the **deployment** timer
`disabled`. `NOTIFIER_NOT_CONFIGURED` means the OCI environment does not
yet contain a valid Telegram token/chat ID. Resolve that in the root-private
environment rather than publishing credentials or enabling deployment gates.

Every three minutes, the notifier looks for root-private OCI request receipts
and verifies the terminal executor result. It declares a **new rollout
successful** only when the terminal receipt says `SUCCEEDED`, the executor
attests `DEPLOYED`, the protected `result.json` audit corroborates it,
and live Git/API SHAs both match the exact requested release. It separately
reports `ALREADY_CURRENT`, failures, stalled `STARTED` receipts, and
signed requests that expired with no host receipt. Unverifiable success
conditions produce a **verification required** alert, never a false success.

Delivery failures are retried on the next notifier timer run. Sent-event
markers on the durable data mount prevent normal duplicate alerts; a power
loss between Telegram delivery and writing the marker can result in one repeat.
Notification errors do not block deployment, and the independent timer runs
even while the deploy authorization is OFF. Completion messages are evidence
and a prompt to inspect the host receipt; they do not authorize any new
deployment or automatically open/close a maintenance window.
