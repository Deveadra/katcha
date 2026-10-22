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
```

Check the variable reads `true`. **This opens the GitHub request gate**, not
the OCI deployment gate.

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
deployed release. Proceed to OCI promptly *after the approval succeeds*;
an expired request requires a new approved dispatch, never relaxed validation.

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

Expected eventual result is a **`DEPLOYED` receipt** for the selected SHA.
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
revision; avoid printing protected environment copies or signing keys.

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
