# Katcha production deployment — GitHub approval, private OCI host-pull

**State:** code merged does not mean deployed. The OCI poller and deployment
gate stay **OFF** until a separately approved maintenance window. Production
runs on the existing OCI A1 host, not WSL or Oracle Cloud Shell.

## 1. Know which terminal you are using

| Prompt | Machine | Correct tasks |
|---|---|---|
| `tycarroll@Chronos` | Windows → **WSL Ubuntu** | `gh`, Bastion tunnel, transfer key, open SSH |
| `ubuntu@katcha-prod-primary-001` | **OCI production VM** | `/opt/katcha`, `sudo git`, installation, Docker, systemd |
| `tyreabc@cloudshell` | Oracle Cloud Shell | **Do not run these commands here** |

There is **no /opt/katcha on WSL**. The SSH key-transfer command below runs
one remote command and immediately returns to **WSL**. It does *not* leave an
interactive OCI shell. Use the separate login in `3` afterward.

Do not paste signing keys, `/etc/katcha/katcha.env`, AWS/OCI credentials or
Cloudflare tokens into chat, issues or logs.

## 2. GitHub Environment protection (browser)

GitHub → `Deveadra/katcha` → **Settings → Environments** → select or create
`katcha-production`, matching that name exactly.

| Setting | Correct choice | Why |
|---|---|---|
| Required reviewers | `Deveadra` or another trusted reviewer | Protected approval |
| Prevent self-review | **On**, if someone *else* can approve. If Deveadra is the only approver and starts the workflow, turn **off**, or add a second reviewer | Otherwise the run cannot be approved |
| Wait timer | Off unless intentionally needed | Not required |
| Allow administrators to bypass configured protection rules | **Uncheck** | Avoid bypass |
| Deployment branches and tags | **Selected branches and tags** → add exactly `main` | *Protected branches only* admits every branch if there are no branch-protection rules |

Click **Save protection rules**. Independently protect `main` with review/
status checks in repository rules.

Settings → **Secrets and variables → Actions**:
- Environment `katcha-production` **secret**:
  `KATCHA_PRODUCTION_DEPLOY_HMAC_KEY`.
- Repository Actions **variable**: `KATCHA_PRODUCTION_DEPLOY_ENABLED=false`.
  Keep it **false during setup**. This is a variable, not a secret.

## 3. Signing key and OCI connection (start in WSL)

**Already completed initial setup? Do not regenerate or re-upload the signing
key.** It must be identical at both ends. First-time setup only:

~~~bash
umask 077
mkdir -p "$HOME/.config/katcha"
test ! -e "$HOME/.config/katcha/production-deploy.key" &&
  openssl rand -hex 32 > "$HOME/.config/katcha/production-deploy.key"
gh secret set KATCHA_PRODUCTION_DEPLOY_HMAC_KEY \
  --repo Deveadra/katcha --env katcha-production \
  < "$HOME/.config/katcha/production-deploy.key"
~~~

Still in **WSL**:

~~~bash
cd ~/src/katcha
bash scripts/katcha-oci-admin.sh connect
~~~

The tunnel should report `CONNECTED` on `127.0.0.1:22022`. To transfer the key
(noninteractive; **returns to WSL**):

~~~bash
ssh -F /dev/null -4 \
  -i "$HOME/.ssh/katcha-oci" \
  -o IdentitiesOnly=yes -o StrictHostKeyChecking=yes \
  -o "UserKnownHostsFile=$HOME/.config/katcha/production/bastion/target_known_hosts" \
  -p 22022 ubuntu@127.0.0.1 \
  'sudo sh -c "umask 077; cat > /etc/katcha/production-deploy.key"' \
  < "$HOME/.config/katcha/production-deploy.key"
~~~

Expected no output. **You are still on WSL**. Now open an interactive OCI
connection, without any remote command:

~~~bash
ssh -F /dev/null -4 \
  -i "$HOME/.ssh/katcha-oci" \
  -o IdentitiesOnly=yes -o StrictHostKeyChecking=yes \
  -o "UserKnownHostsFile=$HOME/.config/katcha/production/bastion/target_known_hosts" \
  -p 22022 ubuntu@127.0.0.1
~~~

**STOP** unless your prompt now reads `ubuntu@katcha-prod-primary-001:~$`.
Never disable SSH host-key verification to make a failed command pass.

## 4. Validate and install on OCI (not WSL)

On the **OCI server**, read-only preflight:

~~~bash
hostname
whoami
sudo stat -c 'Signing key owner=%U mode=%a bytes=%s' /etc/katcha/production-deploy.key
sudo git -C /opt/katcha status --short
sudo git -C /opt/katcha rev-parse HEAD
sudo systemctl is-active katcha
mountpoint /srv/katcha
~~~

Expected: hostname `katcha-prod-primary-001`, user `ubuntu`, key owner
`root`/mode `600`/65 bytes, **empty Git status**, active `katcha` and mounted
`/srv/katcha`. Git SHA can differ from old examples.

After **accepted main CI**, install the root-owned deployment scripts from
accepted main, without upgrading the running checkout. Use this **subshell**
so shell options do not alter your interactive session:

~~~bash
(
  set -euo pipefail
  test "$(hostname)" = "katcha-prod-primary-001"
  cd /opt/katcha
  test -z "$(sudo git -C /opt/katcha status --porcelain)"
  sudo git -C /opt/katcha fetch origin main
  TARGET="$(sudo git -C /opt/katcha rev-parse origin/main)"
  TEMP="$(mktemp --suffix=.sh)"
  trap 'rm -f "$TEMP"' EXIT
  sudo git -C /opt/katcha show \
    "$TARGET:deploy/scripts/install-production-deployer.sh" > "$TEMP"
  test -s "$TEMP"
  bash -n "$TEMP"
  sudo bash "$TEMP" --install
)
~~~

Expected: `DEPLOYER_INSTALLED source_sha=... enablement=OFF
runtime_untouched=true`. Fetch may print twice because the installer also
fetches. If fetch or any command fails, **stop**. Do not change `origin`,
run `git reset`, guess a GitHub SSH fingerprint, or launch a Compose restart.
The installer makes no deployment or service restart.

Verify (OCI):

~~~bash
sudo test -x /usr/local/sbin/katcha-production-deploy && echo "Executor installed"
sudo test -x /usr/local/sbin/katcha-production-deploy-poll && echo "Poller installed"
sudo systemctl list-unit-files 'katcha-production-deploy-poll*'
sudo systemctl is-active katcha
sudo /usr/local/sbin/katcha-production-deploy-poll
~~~

Expected both files executable, `katcha` active, poller
`{"status":"DISABLED"}`, timer **STATE=disabled**. A systemd
`PRESET=enabled` is not evidence the timer is enabled.

**When deployment code changes:** rerun the same `4` installer after merging
and passing CI, to refresh the fixed root-owned copies. **Keep the same
signing key**; the CD-107 poller uses signed-request version 2.

## 5. Migration and Temporal rehearsal (deployment still OFF)

After updating the installer from accepted main, run on OCI:

~~~bash
sudo /usr/local/sbin/katcha-production-deploy --readiness
~~~

This is **read-only** and does not require the production deployment flag.
It validates the current release, exact PostgreSQL Alembic starting revision
`0063_authoritative_search_ledger`, absence of the new table, and every
running Temporal execution for pending activities/child tasks. It never
changes data or cancels workflows.

- `REVIEW_REQUIRED`: read-only checks passed, **not** permission to deploy.
- `BLOCKED`: stop; collect sanitized output for review. A long-running
  workflow may be idle; a *pending activity* means an interruption is unsafe.
- The executor inventories workflows again after its fresh R2 backup/restore
  test and refuses the stop if the set of execution IDs changed.

**Migration 0064 is forward-only for automatic recovery.** It creates
`application_ai_settings` with an approved exact blob and must be accompanied
by independently signed `approve-additive-0064` approval. It is refused by
default. If it runs, old Docker images do not recognize Alembic revision 0064,
so automatic rollback to the old release is **prohibited**. A failure requires
supervised forward repair. Other migrations are rejected.

## 6. Deployment activation (ONLY after separate review)

**Do not run during installation.** Require: full current-main CI and six
verified published images; successful rehearsal; no pending Temporal
activities; fresh three-database R2 restore; verified leadership fence;
approved forward-only migration plan; a maintenance window and recovery
operator available.

Then on **OCI** only, create the distinct root-owned opt-in:

~~~bash
printf '%s\n' 'KATCHA_PRODUCTION_DEPLOY_ENABLED=true' |
  sudo sh -c 'umask 077; cat > /etc/katcha/production-deploy.enabled'
sudo stat -c '%U %a' /etc/katcha/production-deploy.enabled
sudo systemctl enable --now katcha-production-deploy-poll.timer
sudo systemctl list-timers katcha-production-deploy-poll.timer
~~~

Separately set the **GitHub repository Actions variable**
`KATCHA_PRODUCTION_DEPLOY_ENABLED=true`.

GitHub → **Actions → Production deployment → Run workflow**:
select branch `main`, exact current 40-character SHA, and inputs:

- `running_workflows=refuse` by default; `approved-resume` only with a
  reviewed quiescence/idempotence decision.
- `migration_policy=refuse` by default; `approve-additive-0064` only when
  the reviewed 0064 preflight passes. Both policies are signed.
- `confirmation=DEPLOY KATCHA PRODUCTION`.

The protected job publishes a 20-minute signed request, **not a completed
deployment**. The OCI timer polls every five minutes, verifies the request,
runs the fixed executor, and records a root-only receipt.
Confirm local/public release, all containers, workflows, Cloudflare Access
and frontend/navigation/assets afterward. Expect a **full-stack restart**, not
a zero-downtime API-only swap.

## 7. Diagnostics and emergency stop

On OCI:

~~~bash
sudo systemctl status katcha-production-deploy-poll.timer --no-pager
sudo journalctl -u katcha-production-deploy-poll.service -n 60 --no-pager
sudo ls -la /srv/katcha/deployments/requests
curl -fsS http://127.0.0.1:8000/v1/health/release
echo
~~~

Disable new deployment requests:

~~~bash
sudo systemctl disable --now katcha-production-deploy-poll.timer
sudo rm -f /etc/katcha/production-deploy.enabled
~~~

Set the GitHub Actions variable `false` too. A consumed/failed request is
never automatically retried. Inspect `/srv/katcha/deployments` and service
logs without exposing private environment copies.

**Do not** restart `katcha.service`, issue concurrent `docker compose up`,
change the coordinator epoch, downgrade a migrated database, overwrite R2,
reset the Git checkout, or enable OCI paid recovery to repair routine deploys.
Existing PostgreSQL/Temporal histories remain on their durable volumes, but
workflow retries and post-deployment public UI behavior require verification.
