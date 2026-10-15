# Katcha production deployment — GitHub-approved, OCI host-pull

**Status:** Manual GitHub production deployment workflow and root-owned OCI
executor implemented, disabled by default. A one-time installation and
two independent operator opt-ins are required. Neither merging this PR nor
publishing Docker images changes the running OCI production release.

## Why host-pull (not a GitHub runner on OCI)

`Deveadra/katcha` is a **public repository**. A persistent self-hosted
GitHub Actions runner installed on the production VM risks executing untrusted
public-PR workflow code. **No self-hosted runner is permitted on production.**
GitHub-hosted runners also cannot safely reach the private OCI VM without new
privileged access. Instead:

1. GitHub-hosted Actions verifies current `main` CI and GHCR publication.
2. A protected GitHub Environment `katcha-production` requires operator review.
3. The approved Actions job uses an environment-only 256-bit HMAC signing key
   to publish a short-lived, SHA-specific GitHub **Deployment request**.
4. A five-minute, root-owned systemd timer on the existing OCI VM anonymously
   reads GitHub's public deployment feed, verifies the HMAC, exact target,
   host identity, UTC time window, and replay protection, and invokes only
   `/usr/local/sbin/katcha-production-deploy`. The signing key is kept in
   `/etc/katcha/production-deploy.key` (root-owned, mode 0600).
5. The executor independently rechecks release evidence, current OCI authority
   and running containers before any lifecycle operation. Signed requests
   expire after 20 minutes and are consumed at most once, even if execution
   fails or the host restarts. Retry requires a **new** protected GitHub run.

No OCI administrator API key, recovery secret, GitHub PAT, GitHub runner,
new public SSH route, Bastion session, paid instance or Cloudflare bypass is
used for routine deployment requests. A one-time **operator** SSH session
is still required for installer and secret provisioning.

## Before enabling: mandatory one-time operator setup

**1. GitHub review boundaries.** In the repository GitHub UI:

- Settings → Environments → create **`katcha-production`** with required
  reviewer approval, main-only branch restriction and self-review prevention
  where supported. Require branch review for deployment workflow/code changes.
- Settings → Secrets and variables → Actions → Variables → create
  `KATCHA_PRODUCTION_DEPLOY_ENABLED=false`.
- Do not enable until a second operator has reviewed the workflow, scripts,
  backup policy, Temporal workflow safety, and recovery implications.

**2. Generate the same signing key at both ends without posting it anywhere.**
From trusted **WSL Ubuntu** (not Oracle Cloud Shell):

```bash
umask 077
mkdir -p "$HOME/.config/katcha"
openssl rand -hex 32 > "$HOME/.config/katcha/production-deploy.key"
gh secret set KATCHA_PRODUCTION_DEPLOY_HMAC_KEY \
  --repo Deveadra/katcha --env katcha-production \
  < "$HOME/.config/katcha/production-deploy.key"
```

From the same **WSL terminal** with the existing managed OCI Bastion
connection on port 22022, transfer the key without printing it:

```bash
ssh -F /dev/null -4 -i "$HOME/.ssh/katcha-oci" \
  -o IdentitiesOnly=yes -o StrictHostKeyChecking=yes \
  -o "UserKnownHostsFile=$HOME/.config/katcha/production/bastion/target_known_hosts" \
  -p 22022 ubuntu@127.0.0.1 \
  'sudo sh -c "umask 077; cat > /etc/katcha/production-deploy.key"' \
  < "$HOME/.config/katcha/production-deploy.key"
```

This never writes the signing key into GitHub logs or prints it to the chat.
If the known-hosts file or tunnel is missing, stop and follow
[OCI Operator Access](OCI_OPERATOR_ACCESS.md); never disable host verification.

**3. Install the approved code on the existing OCI primary, without touching
the running checkout or containers.** After this PR is merged and its main
CI/image evidence is accepted, in the existing OCI SSH session:

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

The installer copies the root-owned fixed executor, HMAC poller and systemd
units from the reviewed main tree; compiles both Python scripts and reloads
systemd. It does **not** enable the timer or advance the application checkout.
The old unleased Compose owner continues running until an explicitly approved
supervisor stop. After any accepted deployer code update, rerun the reviewed
installer to update the root-owned copies.

**4. Explicitly enable only after review.** On the production host:

```bash
printf '%s\n' KATCHA_PRODUCTION_DEPLOY_ENABLED=true |
  sudo sh -c 'umask 077; cat > /etc/katcha/production-deploy.enabled'
sudo systemctl enable --now katcha-production-deploy-poll.timer
sudo systemctl list-timers katcha-production-deploy-poll.timer
```

Then set the GitHub **repository variable**
`KATCHA_PRODUCTION_DEPLOY_ENABLED=true`. These two switches are
independent. The host poller does no network activity when its local gate is
absent or disabled; enabling it does **not** deploy a release by itself.

## Deploy from GitHub Actions

Actions → **Production deployment** → **Run workflow**:

- Select branch `main`.
- Enter the exact latest 40-character accepted main SHA.
- Choose `running_workflows=refuse` unless a supervised review permits a
  full-stack interruption of active Temporal activities. The override
  `approved-resume` may cause durable workflows to retry; it must **not**
  be interpreted as a guarantee of activity idempotence.
- Type `DEPLOY KATCHA PRODUCTION`.

GitHub validates exact-main CI + image publication and requests approval for
the `katcha-production` environment. The protected job signs a deployment
request and exits **after requesting**, not after finishing, the OCI rollout.
The host timer checks every five minutes and can consume the request within its 20-minute window.
No desktop session, admin reauthentication or SSH tunnel is needed **for
subsequent deployments**.

## What the executor does

- Root-only local enablement and a private exclusive deployment lock;
  active OCI host, mounted durable volume, clean `main` checkout, and
  matching pinned/running old release.
- Independent fresh GitHub release evidence, bounded and descendant commit
  comparison, deny migration/disaster-restore code changes, healthy nine
  running services, Cloudflare coordinator leader/epoch fence and Temporal
  workflow count.
- Stage the exact four ARM64 runtime images, validate source revision labels,
  record image IDs and local registry digest references; compare running
  image IDs after launch to staged IDs.
- Start a **fresh three-database R2 backup and isolated restore test**. Verify
  success result **and new journal completion markers**; a skipped oneshot
  must never count as a backup.
- Reconfirm authority and eligibility immediately before mutation. Stop the
  sole attached `katcha.service` owner, ensure its Compose containers exited,
  acquire PR #421's **same private kernel Compose owner lease**, fast-forward
  the checkout, atomically update the exact global SHA, then release the lease
  so the successor service can obtain it before Compose startup.
- Verify health, the local SHA route, the fence, and Temporal visibility.
  Record pre/post running workflow counts and a protected audit under
  `/srv/katcha/deployments/`.
- On failure, attempt to restore old configuration/images and checkout
  without Git-history deletion, and record `ROLLED_BACK` or
  `RECOVERY_REQUIRED`. **Do not blindly re-dispatch a failed rollout.**
  The absence of migration-file changes does not prove every runtime data
  write is backward-compatible.

**Expected impact:** This is a supervised **full-stack restart**, not a
zero-downtime API-only swap. PostgreSQL data and Temporal histories remain
on the existing durable volume, R2 objects are untouched, and existing
leadership identity/epoch and paid-compute/recovery switches stay unchanged.
Running workflows may pause/retry; require human review of activity
idempotence before selecting `approved-resume`.

**Public acceptance remains separate:** The signed-request job and host
readiness do not automatically verify protected browser navigation,
Cloudflare Access sessions, CSS/assets, media jobs or live AI behavior.
Inspect `https://app.katcha.stream` and the exact public release after
the host reports `DEPLOYED`. Independent signed GHCR digest attestation,
automatic zero-downtime worker draining, and safe unattended schema
migrations require additional implementation before autonomous deployment
on every push should be enabled.

## Inspect / disable / recover

On OCI:

```bash
sudo systemctl status katcha-production-deploy-poll.timer --no-pager
sudo journalctl -u katcha-production-deploy-poll.service -n 60 --no-pager
sudo ls -la /srv/katcha/deployments/requests
curl -fsS http://127.0.0.1:8000/v1/health/release
```

Emergency disable:

```bash
sudo systemctl disable --now katcha-production-deploy-poll.timer
sudo rm -f /etc/katcha/production-deploy.enabled
```

Also set the GitHub repository deployment flag **false**. Do not restart
`katcha.service` or launch competing Docker Compose commands to fix a
stalled deployment. A consumed/failed signed request is never automatically
retried; inspect the protected audit and arrange a new reviewed request.
