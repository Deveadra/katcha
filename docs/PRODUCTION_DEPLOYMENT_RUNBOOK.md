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
It validates the current running release, the **actual** PostgreSQL schema,
and every running Temporal execution for pending activities/child tasks.
It never changes data or cancels workflows.

The reviewed migration boundary is **0063 → 0064 → 0065**. The executor checks
actual PostgreSQL catalogs rather than trusting the Alembic revision alone:

- At `0063_authoritative_search_ledger`, both new tables must be absent.
- At `0064_application_ai_settings`, the settings table must match the approved
  columns and primary key, and the acquisition-jobs table must be absent.
- At `0065_acquisition_jobs`, both tables must match the approved definitions,
  including acquisition-job columns, keys, checks, foreign key and indexes.
  This state is required for migration-free releases whose target head is 0065.

For a release that adds migration 0065, use the separately reviewed policy
`approve-additive-through-0065`. It permits only the exact added immutable
migration files in the accepted comparison: 0065 alone, or the known 0064/0065
chain when the previous checkout predates both. A database already at verified
0064 advances through 0065 without reapplying 0064. Unknown revisions, altered
migration blobs, extra migration changes and schema mismatches remain blocked.
The historical `approve-additive-0064` policy never authorizes migration 0065.

The reviewed blobs are `3d411641d625c772856142d6a8ce43447be7f2b5` for 0064 and
`1a7debea0c609bd3a6a5f19c3b310b64077e9013` for 0065. Migration 0065 only creates
`acquisition_jobs` and its indexes and constraints; it does not modify existing
rows or activate remote acquisition. Its execution setting remains off by default.

Readiness reports the observed starting revision and schema state. It is read-only
and does not authorize deployment. The executor rechecks the same database boundary
immediately before maintenance and verifies the exact target schema afterward.

- `REVIEW_REQUIRED`: read-only checks passed; a deployment still needs its exact
  release, migration and workflow policies.
- `BLOCKED`: stop and collect sanitized diagnostics. A long-running workflow may
  be idle; a pending activity can make interruption unsafe.
- The executor inventories workflows again after fresh backup/restore verification
  and refuses maintenance if the execution set changed.

**Approved additive migration releases are forward-only for automatic recovery.**
Old images do not recognize the new revision. After a migration-bearing release
starts changing production, the executor must preserve the upgraded database and
report `RECOVERY_REQUIRED` on failure, rather than downgrade or automatically
restart incompatible old code. Use a supervised forward repair.

## 6. Deployment activation (ONLY after separate review)

**Do not run during installation.** Require: full current-main CI and seven
verified published images; successful rehearsal; no pending Temporal
activities; fresh three-database R2 restore; verified leadership fence;
approved migration classification (or proof no migration is needed);
a maintenance window and recovery operator available.

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
- `migration_policy=refuse` for a migration-free comparison whose database
  already matches the exact target schema. Use `approve-additive-through-0065`
  only for the reviewed added 0065 file or 0064/0065 chain. The executor verifies
  the actual starting schema, including already-applied 0064, before proceeding.
  The migration policy is part of the signed request; it cannot be changed later.
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

## Remote acquisition handoff schema (0066)

`approve-additive-through-0066` permits only the immutable added file
`0066_acquisition_handoff.py`, starting from a catalog-verified 0065 database.
Earlier migration policies cannot authorize it. This forward-only migration
adds the transfer cursor table and job media/source fields, adds the completed
status, and widens historic byte counters to bigint without narrowing data.
The deployer checks the exact migration blob, job and transfer columns, defaults,
constraints, indexes, and widened byte counters after startup. Migration-free
0066 releases require the same verified schema. Readiness does not activate
remote acquisition; configure and admit a host separately.
