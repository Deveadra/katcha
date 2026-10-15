# Production deployment — GitHub Actions → existing OCI primary

**Status:** Code implemented behind two disabled-by-default permissions and a
one-time, manually registered production runner. Nothing runs on OCI merely
because this workflow is merged. This is a **bounded supervised full-stack
promotion**, NOT a zero-downtime API-only hot swap.

The GitHub Actions workflow named **Production deployment** appears under Actions
after this change merges into `main`. It takes an exact SHA, Temporal workflow
interruption policy and explicit confirmation. **Publish runtime images** builds
artifacts and **Application launcher** tests the local application; neither
deploys to OCI.

## Trust and production ownership

- The existing OCI A1 primary retains its private networking, single active
  deployment identity/epoch, PostgreSQL volume and R2 buckets.
- A **dedicated unprivileged GitHub runner user** `katcha-deploy` on that host
  receives only workflow jobs labelled `katcha-production`. Do not give that
  user the Docker socket/group, the sudo group, or credentials under
  `/etc/katcha`. Limit it to this one repository, never an organization-wide
  shared runner. Do not run pull-request workflows on this runner.
- The workflow's deploy job uses the GitHub Environment `katcha-production`.
  **Configure required reviewer approval, restrict deployments to main, and
  prevent self-approval where available.** Do not remove approval for speed.
- Only the fixed-path root-owned `/usr/local/sbin/katcha-production-deploy`
  may be run through passwordless sudo. The workflow cannot supply a script,
  host, OCI instance, shell command or arbitrary filename.
- Two explicit flags are independently required:
  `KATCHA_PRODUCTION_DEPLOY_ENABLED=true` as a GitHub **repository variable**
  and the root-only file `/etc/katcha/production-deploy.enabled`, with exactly
  that one line, ownership root and permissions 0600. Both **default OFF**.
- There is no OCI admin token, GitHub recovery key, Cloudflare bypass token,
  Bastion network change, new compute, or paid fallback in this workflow.
  Initial runner installation requires a one-time operator SSH session.
  Subsequent manual deployments do **not** require daily Bastion sessions.
- A repository compromise remains high impact even with an unprivileged
  runner. Protect main, require code review for this workflow and deploy scripts,
  and keep the host gate disabled outside approved maintenance windows.

## One-time setup (operator-owned)

1. In GitHub → **Settings → Environments**, create `katcha-production`.
   Require reviewer approval and main-only deployment branch protection. In
   **Settings → Secrets and variables → Actions → Variables**, keep
   `KATCHA_PRODUCTION_DEPLOY_ENABLED` **false** until fully configured.
2. On the verified OCI production VM, create the isolated service account
   (only once):
   ```bash
   id katcha-deploy || sudo useradd --create-home --shell /bin/bash katcha-deploy
   id -nG katcha-deploy
   ```
   Verify it does **not** belong to `sudo`, `docker`, or `root` groups.
3. GitHub → **Settings → Actions → Runners → New self-hosted runner**.
   Select **Linux / ARM64**. Under the account `katcha-deploy`, in its own
   `/home/katcha-deploy/actions-runner` directory, follow GitHub's
   displayed exact download/checksum/registration instructions. Scope it only
   to `Deveadra/katcha` and give it the additional custom runner label
   **katcha-production**. Start it as a systemd service using GitHub's
   documented `svc.sh install katcha-deploy` procedure. Never place the
   runner under `/opt/katcha` or in the Docker group.
4. From an existing operator SSH session on
   `ubuntu@katcha-prod-primary-001`, after the PR is reviewed/merged and
   exact-head CI succeeds, fetch accepted main **without changing the live
   checkout** and install the reviewed root-owned executor:
   ```bash
   cd /opt/katcha
   sudo git fetch origin main
   TARGET="$(sudo git -C /opt/katcha rev-parse origin/main)"
   TEMP="$(mktemp --suffix=.sh)"
   sudo git -C /opt/katcha show "$TARGET:deploy/scripts/install-production-deployer.sh" > "$TEMP"
   bash -n "$TEMP"
   sudo bash "$TEMP" --install
   rm -f "$TEMP"
   ```
   The installer validates the runner user, compiles the executor, installs a
   narrowly scoped sudoers entry with `visudo`, and leaves the app running.
   It does not create or enable the production gate.
5. Once the environment approvals, restricted runner, backups, and CI are
   reviewed, explicitly enable **both** gates:
   ```bash
   printf '%s\n' 'KATCHA_PRODUCTION_DEPLOY_ENABLED=true' | \
     sudo install -o root -g root -m 0600 /dev/stdin /etc/katcha/production-deploy.enabled
   sudo ls -l /etc/katcha/production-deploy.enabled
   ```
   Set the GitHub repository variable `KATCHA_PRODUCTION_DEPLOY_ENABLED=true`
   separately. That change does not itself trigger deployment.

## Deploy from the GitHub UI

Actions → **Production deployment** → **Run workflow**:

- Branch: **main**.
- `release_sha`: exact latest full 40-character accepted `main` commit.
- `running_workflows`: `refuse` by default. If any active Temporal workflows
  exist, the job fails before mutation. `approved-resume` explicitly permits
  a bounded restart while up to 100 running workflows remain; use it only after
  reviewing potential retries, activity idempotency, and any in-progress media
  work. Approval does **not** make non-idempotent activities safe.
- `confirmation`: exactly `DEPLOY KATCHA PRODUCTION`.

After the GitHub-hosted preflight passes, the protected `katcha-production`
environment requests reviewer approval. The restricted OCI runner then runs
the fixed installed executor through sudo.

The executor independently rechecks main, exact successful push CI and GHCR
publication, ancestry and changed-file list, root-only gate, mounted durable
disk, clean checkout, current release, healthy nine-container inventory,
Cloudflare coordinator fence and Temporal count. **All migration changes are
blocked** until a separate tested migration-safe rollout mechanism exists.

It downloads immutable ARM64 images and validates source labels/digests while
the old stack remains live. It runs a fresh PostgreSQL backup and isolated
restore verification. With authority and acceptance still valid, it stops the
single attached supervisor, waits for the old stack to exit, performs an
ff-only checkout update, atomically pins the new SHA, restarts the existing
systemd service, checks nine containers, local release identity, and fence.
A service interruption is expected; Temporal histories persist in PostgreSQL.

On failed readiness, it attempts a non-destructive rollback to the old pinned
images and checkout, preserving Git history and data. If rollback fails, it
stops and reports `RECOVERY_REQUIRED`; do not attempt repeat deployments.
Result and protected pre-change env copy are retained under
`/srv/katcha/deployments` (root-only). After a rollback, the checkout may be
detached at the old commit and needs explicit operator reconciliation.

**Public Cloudflare Access browser checks remain necessary after a successful
host rollout**: `app.katcha.stream` authentication, navigation, data, asset
HTTP responses, and AI/media tasks. Host readiness alone does not verify them.

## Hard stops

- No job can mutate OCI if either deployment flag is absent/false, target SHA
  is stale, approved workflow evidence is missing, backup/restore fails, or
  the coordinator fence rejects authority.
- Migrations or unknown/oversized Git comparisons are refused; this code is
  not an unattended migration executor.
- Never manually start another `docker compose up` alongside the attached
  supervisor; never force-push/reset the Git checkout; never replace R2 or
  PostgreSQL state from local files; never enable OCI recovery or paid fallback
  to deploy an ordinary release.
- If there is no eligible, online, dedicated self-hosted runner, the deploy job
  will queue rather than use an unsafe GitHub-hosted tunnel. Verify the runner
  is connected before dispatching.
- Keep `KATCHA_PRODUCTION_DEPLOY_ENABLED=false` and remove the host gate
  during prolonged downtime or until all launch safeguards have been audited.
