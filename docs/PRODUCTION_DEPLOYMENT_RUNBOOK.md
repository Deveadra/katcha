# Katcha production deployment — GitHub approval, private OCI host-pull

**Routine release:** Start with [Manual production deployment: repeatable checklist](PRODUCTION_MANUAL_DEPLOYMENT_RUNBOOK.md). It keeps the deployment timer disabled and consumes one signed request manually. This longer document covers first-time setup and exceptional maintenance.

**State:** merging code publishes tested images but does not deploy them.
The separately approved, signed host-pull deployment gate and timer stay **OFF**
between maintenance windows. The read-only release observer can be enabled
independently to check drift hourly; it never promotes or restarts production.
Production runs on the existing OCI A1 host, not WSL or Oracle Cloud Shell.

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
  **Do not define the same variable inside the `katcha-production`
  Environment.** Preflight sees the repository gate while protected approval
  may see a conflicting environment-scoped value. Confirm no duplicate exists
  using `gh variable list --repo Deveadra/katcha --env katcha-production`.
  Run `bash scripts/check-production-deployment-github.sh` from WSL
  after temporarily enabling the repository gate and **before dispatch**.
  Do not disable the gate until the protected authorization publishes its
  signed request. For a repeat deployment, use the linked manual runbook.

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

After installing the root-owned deployer tools from accepted main (Section 4),
inspect an **exact immutable reviewed candidate**, either current main or a
previous SHA that remains in accepted main ancestry:

~~~bash
sudo git -C /opt/katcha fetch origin main
TARGET="$(sudo git -C /opt/katcha rev-parse origin/main)"
sudo /usr/local/sbin/katcha-production-deploy --plan-release "$TARGET"
~~~

The example selects the latest main; to promote a separately reviewed
immutable ancestor, use that approved exact SHA for TARGET instead. The planner
checks the running database and Temporal state, bounded changed-file and commit
comparison, signed-release eligibility prerequisites and reviewed migration
blobs. It returns a **candidate** `candidate_migration_policy`, active workflow
count and `deployment_authorized: false`; it never grants approval or starts
deployment. A `BLOCKED` result must not be bypassed with guessed policy values.

**Operator evidence (2026-10-21):** production checkout
`a8fa8a877f81fc616af4d2eebbcd21b44f3bc17d`, verified
`0065_acquisition_jobs`, six running Temporal workflows with pending-activity
check passed, and the deployment timer off. The comparison at inspection time
was 32 commits and 55 changed files, including only added migration `0066`.
This is within the comparison limits (250 commits, fewer than 300 changed
files), but the six executions still require a reviewed resume decision.
Re-run the planner for the final approved SHA before deployment.

The reviewed migration boundary is **0063 → 0064 → 0065 → 0066**. The executor checks
actual PostgreSQL catalogs rather than trusting the Alembic revision alone:

- At `0063_authoritative_search_ledger`, both new tables must be absent.
- At `0064_application_ai_settings`, the settings table must match the approved
  columns and primary key, and the acquisition-jobs table must be absent.
- At `0065_acquisition_jobs`, both tables must match the approved definitions,
  including acquisition-job columns, keys, checks, foreign key and indexes.
  This is the required base for the approved 0066-only upgrade.
- At `0066_acquisition_handoff`, the acquisition handoff catalog, widened byte
  counters, and transfer table must match the reviewed schema for a
  migration-free release targeting a checkout with 0066.

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

For the **0065 → 0066** transition, the independently reviewed candidate
policy is `approve-additive-through-0066` if the release comparison adds only
`0066_acquisition_handoff.py` and the running database matches 0065 exactly.
The migration blob is pinned to
`5e5facd8557a97ab788d529f0b40b8740b414df2`. No policy authorizes an
unreviewed 0063/0064 → 0066 jump or an arbitrary future migration. Do not
manually stamp revisions or skip catalog checks.

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

**Do not run during installation.** Require: successful push-to-main CI and all
seven verified published images **for the exact pinned release commit**;
successful rehearsal; no pending Temporal
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
select workflow branch `main`, but set `release_sha` to the **reviewed,
immutable 40-character candidate commit**. The candidate must still be on
accepted `main` ancestry and be a descendant of the currently deployed commit;
it need not equal the current branch tip. New development merges do not change
the release selection, CI or image identities. The GitHub approval job and OCI
executor re-verify ancestry, SHA-specific push CI and full image publication.
Never use `:main` Docker tags or an unverified PR commit as the candidate.

**Installer versus app release:** the root-owned installer always installs the
latest reviewed deployment tooling from `origin/main`, but it does not move
the running production release. Its `DEPLOYER_INSTALLED source_sha` is therefore
not necessarily the `release_sha` you later choose to deploy.

Before submitting inputs, confirm the read-only `--plan-release` response
matches the exact immutable `release_sha`, candidate migration policy, and
current workflow inventory. Its recommendation is not approval.

Submit the following inputs:

- `running_workflows=refuse` by default; `approved-resume` only with a
  reviewed quiescence/idempotence decision.
- `migration_policy=refuse` for a migration-free comparison whose database
  already matches the exact target schema. Use `approve-additive-through-0065`
  only for the reviewed added 0065 file or 0064/0065 chain.
  For a reviewed 0065 → 0066 release that adds
  `migrations/versions/0066_acquisition_handoff.py`, use
  `approve-additive-through-0066` only after checking the exact 0065 starting
  schema, the immutable 0066 migration blob and the forward-repair plan.
  The executor verifies the actual starting schema. The migration policy is
  part of the signed request; it cannot be changed later.
- `confirmation=DEPLOY KATCHA PRODUCTION`.

The protected job publishes a 20-minute signed request, **not a completed
deployment**. The OCI timer polls every five minutes, verifies the request,
runs the fixed executor **for that exact candidate SHA (not the moving main
tip)**, and records a root-only receipt. Compare the signed SHA to the serving
release and receipt before declaring success.
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


## 8. Prevent drift without automatically deploying

After the next reviewed production rollout has placed the observer files in
`/opt/katcha`, the separate
[read-only release observer](PRODUCTION_RELEASE_OBSERVER.md) may be opted in
for hourly comparison and a structured journal report. It emits
`cadence_review_due: true` at 75 commits or 100 changed files, well before
the fail-closed 250-commit / 299-file limit. Arrange a protected incremental
release review while the delta remains small.

Observer output in journald is not itself a push notification. Keep the
production deployment gate and deployment timer disabled between explicitly
approved releases. Automatic image publication and observer checks do not
authorize schema changes or restarting Temporal workflows.

## Reviewed acquisition recovery migration0067

For a release moving a verified0066 database to0067, use the bounded
[`approve-additive-through-0067` review](PRODUCTION_0067_MIGRATION_REVIEW.md).
This adds the two recovery tables and expands acquisition status; it preserves
all existing signing, Temporal drain, backup/restore and exact-release gates.
