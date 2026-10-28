# Katcha: manual production deployment (operator runbook)

**Purpose:** Deploy **one explicitly reviewed immutable release** to the existing OCI production primary. This is **manual**, not continuous deployment. First complete, verified release: **2026-10-28** (receipt below).

**The five-stage path:** **Select → Review → Authorize → Deploy once → Verify and close.**

> **Safety:** A successful GitHub Action authorizes a *signed request*, not a deployed application. Never bypass a `BLOCKED` result, assume `REVIEW_REQUIRED` means approval, force a migration, prune Docker volumes, reset production Git, or run Compose outside the systemd supervisor.

## Know which computer you are on

| Place | Prompt/example | Run |
| --- | --- | --- |
| **WSL (Chronos)** | `tycarroll@Chronos:~/src/katcha$` | `git` / `gh`, Bastion connection, GitHub gate |
| **OCI production** | `ubuntu@katcha-prod-primary-001:~$` | `sudo`, systemd, Docker, database and deployer |
| **GitHub browser** | Actions → Production deployment | Protected Environment approval |

Your Windows PC's disk does **not** host Katcha. On OCI, Docker/containerd use the **system disk** (`/`), while durable data is mounted separately at `/srv/katcha`. Free space on one does not make up for a full disk on the other.

**Do not repeat one-time setup for a routine release.** For missing GitHub Environment, signing key, OCI account/Bastion, or deployer, use the [infrastructure/setup runbook](PRODUCTION_DEPLOYMENT_RUNBOOK.md) and [OCI operator access](OCI_OPERATOR_ACCESS.md). Never regenerate an existing signing key just to complete a deployment.

## Operator worksheet (record, do not paste secrets)

| Item | Record |
| --- | --- |
| Target exact 40-character application SHA | |
| Current production SHA | |
| Matching push-to-main CI ID / seven-image publication ID | |
| Database revision, migration files, approved migration policy | |
| Temporal count/IDs/types, pending checks, approved workflow policy | |
| Most recent backup and isolated three-database restore times (UTC) | |
| OCI system-disk free space and projected image staging | |
| GitHub signed request ID/time; expiry | |
| Protected OCI `result.json` path, deployed SHA and Temporal recovery | |
| Authenticated browser smoke test and known issues | |

## 1. Select an immutable release (WSL)

```bash
cd ~/src/katcha
git fetch origin main
git rev-parse origin/main
```

Write down the complete SHA. In [Actions](https://github.com/Deveadra/katcha/actions), check **two successful workflows for that exact SHA**: (a) `CI` with event **push** to `main`, and (b) `Publish runtime images` with **seven successful image jobs** and anonymous pull validation. A passing PR build or `:main` image tag is not enough. From WSL, list matching runs if helpful:

```bash
TARGET_SHA='PASTE_EXACT_40_CHARACTER_SHA'
gh run list --repo Deveadra/katcha --commit "$TARGET_SHA" --limit 30
```

An *older*, fully CI-published commit still on accepted `main` ancestry may be selected when newer merges have not finished building. **Lock the SHA in the worksheet.** New `main` merges do **not** silently change this attempt's target. If the **production host** advances to a different release, stop and repeat production-relative checks.

**STOP:** Without successful matching push CI and published images, do not request deployment. Repair the failed workflow and reselect a verified SHA.

## 2. Review the host and release (no application changes)

### 2A. Reach OCI from WSL

```bash
cd ~/src/katcha
bash scripts/katcha-oci-admin.sh status
bash scripts/katcha-oci-admin.sh connect
```

If asked to authenticate, run `bash scripts/katcha-oci-admin.sh reauth` and retry. Resolve IP allowlist/Bastion trouble through [OCI operator access](OCI_OPERATOR_ACCESS.md), not relaxed SSH host-key checks.

From **WSL**, open the host:

```bash
ssh -F /dev/null -4 \
  -i "$HOME/.ssh/katcha-oci" \
  -o IdentitiesOnly=yes -o StrictHostKeyChecking=yes \
  -o "UserKnownHostsFile=$HOME/.config/katcha/production/bastion/target_known_hosts" \
  -p 22022 ubuntu@127.0.0.1
```

**STOP** unless the new shell really is `ubuntu@katcha-prod-primary-001`. Keep WSL available separately; shell variables are not shared between WSL and OCI.

### 2B. Host safety and storage (OCI, read-only)

```bash
hostname
whoami
sudo git -C /opt/katcha rev-parse HEAD
sudo git -C /opt/katcha status --porcelain
sudo systemctl is-active katcha
sudo systemctl is-enabled katcha-production-deploy-poll.timer || true
sudo test ! -e /etc/katcha/production-deploy.enabled &&
  echo 'Host deployment gate OFF' || echo 'STOP: Host gate present'
sudo stat -c 'Signing key owner=%U mode=%a bytes=%s' \
  /etc/katcha/production-deploy.key
mountpoint /srv/katcha
df -h / /srv/katcha
sudo docker info --format 'DockerRootDir={{.DockerRootDir}}'
```

**PASS:** Correct host/user, clean checkout (no output from `status --porcelain`), Katcha `active`, deploy poll timer `disabled`, host permission file **absent**, signing key root-owned with mode `600` and 65 bytes (never print contents), durable mount present, and adequate disk headroom.

**Disk warning:** The first verified deployment consumed approximately **7.8 GB** staging images; it ended with **3.2 GB free (94% used)**. For a similarly sized *unstaged* release, aim for **at least 13 GB free on `/`** before pulling, leaving roughly 5 GB afterward. This is an **operator guideline, not a built-in deployer threshold**. Measure the expected shared layers when possible; target 10–20% free after staging. If headroom is inadequate, fix it **before** signing.

To investigate OCI disk usage without changing anything:

```bash
sudo du -xhd1 /var/lib/containerd /var/lib/docker 2>/dev/null | sort -h
sudo docker ps --format 'table {{.Names}}\t{{.Image}}'
sudo docker system df -v
```

For an old, **noncurrent, noncandidate, nonrollback** image, verify no container references its *exact reference* (`sudo docker ps -aq --filter "ancestor=IMAGE_REFERENCE"`) and remove only that reviewed reference with `sudo docker image rm IMAGE_REFERENCE`. Docker may share layers; the reclaim amount is not guaranteed. **Never** `docker system prune`, `docker volume prune`, delete containerd files by hand, or delete database/Temporal volumes. A pre-existing host gate is also a **STOP**: inspect prior requests, close it safely, and repeat readiness before authorizing anything.

### 2C. Verify the exact release (OCI, read-only)

**Set the target again on OCI**, using the same SHA recorded on WSL:

```bash
TARGET_SHA='PASTE_SAME_EXACT_40_CHARACTER_SHA'
(
  set -o pipefail
  sudo /usr/local/sbin/katcha-production-deploy \
    --diagnose-release "$TARGET_SHA" | python3 -m json.tool
)
sudo python3 \
  /usr/local/lib/katcha-production-deploy/release_eligibility.py \
  "$TARGET_SHA"
```

| Result | Meaning |
| --- | --- |
| `PASS` | That named technical check passed. |
| `ELIGIBLE_FOR_PREDEPLOY_REVIEW` | Exact-SHA CI, images, ancestry accepted; **not** operator authorization. |
| `REVIEW_REQUIRED` | Normal for backups, Temporal and GitHub protected approval. Inspect `checks[]` and `next_actions[]`, decide below. |
| `BLOCKED` | **STOP.** Follow the named reason and remediation, then repeat the exact-SHA doctor. |

The `installed_tool_version.source_sha` may differ from the application `TARGET_SHA`; it identifies root-owned **deployer tooling**. The legacy diagnostic field `migration` can show a supported baseline; use `database_start_revision` / `release_plan.database_revision` for the *actual* live schema.

**Migration policy:** If `schema_change=false` and `migration_files=[]`, choose **`migration_policy=refuse`**. When `0066_acquisition_handoff` is already applied and verified, do **not** apply it again. For a proposed migration, inspect the exact diff/blob and live database, evaluate compatibility and forward repair, and separately authorize only the proposed reviewed policy. If schema or migration identity differs, **STOP**. No manual Alembic stamp, schema downgrade or forced rollback.

**Temporal policy:** Default **`running_workflows=refuse`** blocks deployment when workflows remain running. Review actual IDs/types and pending work using:

```bash
(
  set -o pipefail
  sudo docker exec katcha-production-temporal-1 \
    temporal --output json workflow list \
    --address temporal:7233 --namespace default \
    --query 'ExecutionStatus="Running"' --limit 101 |
    python3 -m json.tool
)
```

Approve **`approved-resume`** *only* after confirming workflow identities, pending-state checks and candidate replay/idempotency compatibility. The Oct 28 deployment had **six** recurring scheduler workflows, but future deployments may differ. For first single-host activation the executor only permits scheduler types (`AutomaticResearchWorkflow`, `TopicWatchScheduleWorkflow`, `ChannelIntelligenceScheduleWorkflow`), requires no in-flight tasks/children/activities, and rechecks the exact inventory before cutover. **Never terminate workflows to bypass a rejection.**

**Backup/restore:** Verify dated positive evidence and whether more recent attempts failed:

```bash
sudo systemctl show katcha-backup.service katcha-restore-test.service \
  -p Id -p ActiveState -p Result -p ExecMainStatus --no-pager
sudo journalctl -u katcha-backup.service --since '36 hours ago' \
  --no-pager -o short-iso |
  grep -E 'Katcha PostgreSQL backup completed:|Failed|failed' | tail -n 12
sudo journalctl -u katcha-restore-test.service --since '10 days ago' \
  --no-pager -o short-iso |
  grep -E 'Katcha disaster restore test passed for 3 databases|Failed|failed' |
  tail -n 12
sudo systemctl is-enabled katcha-restore-test.timer
```

The restore timer is **weekly**: an empty 48-hour journal isn't proof of failure. Confirm the latest isolated restore passed for **three databases**, the backup completion marker, enabled timer, and no later failure. See [Disaster Recovery Backups](DISASTER_RECOVERY_BACKUPS.md). Historic journal evidence does not replace the executor's **fresh backup and isolated restore checks before stopping production**. If evidence is missing/stale/failed, **STOP** and investigate; don't restore over production.

**Decision gate:** Complete the worksheet, document any migration policy other than `refuse`, independently approve `approved-resume` if needed, verify capacity and a recovery operator, and confirm leadership/fencing readiness. Only then proceed.

**Deployer installation is NOT a routine step.** If `--diagnose-release` is unavailable because tools are outdated, use the [one-time tool installation procedure](PRODUCTION_DEPLOYMENT_RUNBOOK.md) *after review*, and redo all checks. The current installer fetches **live `origin/main` internally**; freeze new merges, verify its installed `source_sha` and do not assume it is pinned to your `TARGET_SHA`. Future engineering should make this explicitly SHA-pinned.

## 3. Authorize ONE signed request (WSL and GitHub)

With Stage 2 approved, and an OCI SSH session ready, on **WSL**:

```bash
cd ~/src/katcha
gh auth status
gh variable set KATCHA_PRODUCTION_DEPLOY_ENABLED \
  --repo Deveadra/katcha --body true
bash scripts/check-production-deployment-github.sh
```

Expect `GITHUB_DEPLOY_AUTH_SCOPE_OK` (repo gate true; no duplicate `katcha-production` Environment gate; protected `KATCHA_PRODUCTION_DEPLOY_HMAC_KEY` secret present *by name*). The helper cannot confirm secret/key equivalence. If it fails, inspect its specific message. Do not regenerate keys or expose contents. A duplicate Environment variable must be investigated separately, not confused with the repository-level gate.

Open [**Actions → Production deployment**](https://github.com/Deveadra/katcha/actions/workflows/production-deployment.yml) → **Run workflow**:

| Input | Value |
| --- | --- |
| Branch | `main` |
| `release_sha` | Locked exact 40-character SHA |
| `running_workflows` | `refuse` or explicitly reviewed `approved-resume` |
| `migration_policy` | `refuse` if no schema change; else separately approved reviewed policy |
| `confirmation` | `DEPLOY KATCHA PRODUCTION` |

Submit; when GitHub asks to review the protected `katcha-production` Environment, click **Review deployments → Approve and deploy**. Wait until both `preflight` and `authorize-request` succeed. Open `authorize-request` logs and find the **exact target**:

```text
Production deployment request published: id=REQUEST_ID sha=YOUR_EXACT_SHA host=existing-oci-primary
```

**Record the request ID/time.** It is signed and valid for **20 minutes**, but **has not deployed anything yet**. A green run summary alone is not evidence that the request was published.

**Immediately after publication, close the GitHub request gate (WSL):**

```bash
gh variable set KATCHA_PRODUCTION_DEPLOY_ENABLED \
  --repo Deveadra/katcha --body false
gh variable get KATCHA_PRODUCTION_DEPLOY_ENABLED \
  --repo Deveadra/katcha
```

Expect `false`. Do **not** close the gate before successful publication, or leave it `true` after a failed/abandoned request. On expiry, issue a newly protected request only after reviewing current state; never bypass timing/signature controls.

## 4. Consume the request exactly once (OCI)

**This block deliberately changes production.** Only run after the protected approval, backup/Temporal/migration decisions, and closed GitHub gate. On **OCI**:

```bash
(
  set -euo pipefail
  test "$(hostname)" = "katcha-prod-primary-001"
  sudo systemctl disable --now katcha-production-deploy-poll.timer
  trap 'sudo rm -f /etc/katcha/production-deploy.enabled' EXIT
  printf '%s\n' 'KATCHA_PRODUCTION_DEPLOY_ENABLED=true' |
    sudo sh -c \
      'umask 077; cat > /etc/katcha/production-deploy.enabled'
  sudo /usr/local/sbin/katcha-production-deploy-poll
)
```

The executor independently checks signed request, strict immutable images, schema, leadership/fencing, Temporal inventory and fresh backup/restore **before stopping the running app**. Do not interrupt, rerun a failed request or manually restart Compose. Expected console result:

```json
{"deployment_request_id": 1234567890, "sha": "YOUR_EXACT_SHA", "status": "SUCCEEDED"}
```

`SUCCEEDED` must be corroborated by the protected `DEPLOYED` audit and live runtime in Stage 5.

## 5. Verify, close both gates and test the web app

On **OCI**, set the *same* target SHA again if this is a new shell:

```bash
TARGET_SHA='PASTE_SAME_EXACT_40_CHARACTER_SHA'
sudo systemctl is-active katcha
sudo git -C /opt/katcha rev-parse HEAD
curl -fsS http://127.0.0.1:8000/v1/health/ready
echo
curl -fsS http://127.0.0.1:8000/v1/health/release
echo
sudo /usr/local/sbin/katcha-production-deploy --readiness
sudo systemctl is-enabled katcha-production-deploy-poll.timer || true
sudo test ! -e /etc/katcha/production-deploy.enabled &&
  echo 'Host deployment gate OFF' || echo 'STOP: Host gate still present'
TARGET_PREFIX="$(printf '%.12s' "$TARGET_SHA")"
sudo find /srv/katcha/deployments -maxdepth 2 -type f \
  -path "*-$TARGET_PREFIX/result.json" -print -exec cat {} \;
df -h /
```

**All must agree:** Katcha `active`; Git checkout and local API `release_sha` = target; ready endpoint `status: ok`; database starting revision correct; Temporal pending-state `PASSED`; protected `result.json` says **`status: DEPLOYED`**, matching SHA and recorded `predeploy_running_workflows`, `postdeploy_running_workflows` and `predeploy_workflow_recovery` (`running`, `completed`, `continued_as_new`); timer `disabled`; host gate absent. The executor also checks the original workflow IDs during recovery. On **WSL**, verify GitHub variable is still `false` with the `gh variable get` command above.

The read-only `--readiness` command may still return `REVIEW_REQUIRED` even after a successful deployment; interpret the named technical fields, not that summary as a failure.

Finally open [app.katcha.stream](https://app.katcha.stream) through **authenticated Cloudflare Access**. Check Channel Studio, sources, queue, activity, review/player and channel data. A public curl redirect to Cloudflare login is normal. A loading web app is a *basic smoke test*, not proof every button works or that autonomous YouTube publishing succeeded. Note any playback stutter or broken feature separately. Record disk left after staging; if critical, schedule capacity remediation before the **next** release.

## STOP decisions and safe remediation

**Emergency stop without shutting down Katcha:** On WSL set `KATCHA_PRODUCTION_DEPLOY_ENABLED=false`. On OCI:

```bash
sudo systemctl disable --now katcha-production-deploy-poll.timer
sudo rm -f /etc/katcha/production-deploy.enabled
sudo systemctl is-active katcha
sudo git -C /opt/katcha rev-parse HEAD
curl -fsS http://127.0.0.1:8000/v1/health/release || true
echo
sudo journalctl -u katcha.service -n 100 --no-pager
sudo find /srv/katcha/deployments -maxdepth 2 -name result.json -print
```

The manually invoked poller's primary output is in **your terminal**, not necessarily a systemd journal. Read only the relevant `result.json`. **Never** print signing keys, `production-env.before`, secrets or raw request payloads.

| Result / issue | What to do |
| --- | --- |
| GitHub `preflight` failed | Check exact SHA ancestry/CI/images, branch `main`, confirmation, and repository gate still `true` through publication. Fix and request fresh protected approval. |
| `GITHUB_DEPLOY_GATE_DISABLED` | Check repository variable versus same-named Environment duplicate; no bypass. |
| `GITHUB_DEPLOY_SIGNING_SECRET_INVALID` | Inspect Environment secret *metadata* and matching existing OCI key; do not regenerate blindly. |
| `NO_VALID_PENDING_REQUEST` / 20-minute expiry | Leave OCI gate OFF; review timing and publish a **new** protected request if still safe. |
| `BLOCKED` / `FAILED_REVIEW_REQUIRED` | Stop; inspect poller message, on-host doctor, service logs and protected receipt. Correct the named blocker, then redo exact-SHA preflight before a new request. |
| `ROLLED_BACK` | Verify current app and DB; checkout might be detached. Do not reset/rebase manually; reconcile under supervision. |
| `RECOVERY_REQUIRED` | **Never blindly retry/rollback.** Preserve PostgreSQL/Temporal/R2. Inspect actual schema and live service, then create a scenario-specific supervised *forward* repair. This conservative status does **not** prove migration ran. |
| Full OCI system disk | Identify containerd layers and Docker image references. Preserve current, candidate, rollback and all persistent volumes. Remove only reviewed unused image references, never automatic prune. |
| GitHub `SUCCEEDED` but browser bad | Prove `DEPLOYED` receipt + matching Git/API/DB/Temporal, then debug Cloudflare/app feature separately; no repeat deploy without evidence. |

Recovery references: [database backup / isolated restore](DISASTER_RECOVERY_BACKUPS.md), [full deployment and credentials](PRODUCTION_DEPLOYMENT_RUNBOOK.md), [OCI Bastion](OCI_OPERATOR_ACCESS.md). **Those are not a tested scenario-specific forward-repair guide**. Escalate unknown recovery states instead of experimenting on the primary.

## Optional tooling and alerts (never deployment authorization)

- GitHub `notify-review` currently uses repository Actions secrets `KATCHA_TELEGRAM_OCI_BOT_TOKEN` and `KATCHA_TELEGRAM_OCI_CHAT_ID` plus optional `KATCHA_TELEGRAM_THREAD_ID`. It only alerts about a real Environment review; its absence is not an authorization bypass. The protected HMAC secret remains separate.
- The OCI `katcha-production-deploy-notify.timer` is a **separate optional notifier**, sourced from root-private `KATCHA_TELEGRAM_BOT_TOKEN` / `KATCHA_TELEGRAM_CHAT_ID`. It may be enabled independently; the **deploy-poll timer remains disabled**.
- [Operations Health](OPERATIONS_HEALTH.md) and [release observation](PRODUCTION_RELEASE_OBSERVER.md) can warn about failures **when actually installed and enabled**; never assume monitoring is active without evidence.
- **Remaining engineering gap:** `install-production-deployer.sh` fetches live `origin/main`; a documented SHA does not prevent an intervening merge changing its tool source. Restrict merges, inspect installer, verify reported `source_sha`, then redo doctor. Add a pinned-SHA install option separately.

## First verified manual deployment: historical acceptance record (not future inputs)

| Evidence on 2026-10-28 | Confirmed result |
| --- | --- |
| Target application | `07aa62eb6223122ec264f71a724e47ec826da85f` |
| Previous production | `a548135326f652a4dbb7e35d2acec8e9f1f431a6` |
| [Push CI](https://github.com/Deveadra/katcha/actions/runs/40617931683) | Success |
| [Runtime images](https://github.com/Deveadra/katcha/actions/runs/40618762728) | Success, seven builds |
| [Protected authorization](https://github.com/Deveadra/katcha/actions/runs/40645819583) | Success, signed request `7379010051` |
| On-host poller and protected audit | `SUCCEEDED`; `DEPLOYED` at `/srv/katcha/deployments/20261028T065324Z-07aa62eb6223/result.json` |
| Post-deployment | Exact Git/API match, readiness healthy, `0066_acquisition_handoff` unchanged |
| Temporal | 6 running before, 6 after; six recovered running |
| Safety gates | GitHub `false`, host gate absent, poll timer `disabled` |
| Browser | App accessible and generally functional; choppy playback still noted |
| OCI capacity after staging | **3.2 GB free, 94% full**: follow-up required |

**This proves the manually signed and verified release path. It does not yet prove end-to-end autonomous acquisition, editing, rendering or YouTube publishing.**
