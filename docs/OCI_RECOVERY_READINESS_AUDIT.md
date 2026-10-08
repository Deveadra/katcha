# OCI recovery readiness: read-only evidence audit

This runbook checks IAM **metadata** and the current primary's backup/restore logs without reading Vault secret contents, starting timers, creating containers, mounting devices, changing the Cloudflare coordinator, or launching a replacement instance.

## Interpreting the two layers

- An IAM policy statement and matching rule being present is necessary but **does not prove** an OCI candidate can fetch a bundle via instance-principal auth. That requires controlled live candidate acceptance later.
- A green `Disaster backup recovery` GitHub workflow is a repository test. It does **not prove** a production R2 backup exists.
- An enabled `katcha-backup.timer` schedules hourly runs; a journal success line is additional evidence that the production upload completed.
- `katcha-restore-test.timer` runs Sundays at 04:15 UTC plus up to 30 minutes of jitter. If not yet fired, a missing restore success marker means **unverified**, not healthy.
- The production restore service uses an isolated temporary PostgreSQL 16 container. This audit does not start it.

## Prerequisites

Use the OCI CLI isolated by Katcha at `~/.cache/katcha/oci-cli-3.94.1/bin/oci`, GitHub CLI authenticated for `Deveadra/katcha`, and the separate short-lived `KATCHA_VAULT_ADMIN` session. Oracle sessions expire; the earlier session was valid only until 2026-10-07 21:42:54 America/Chicago. Authenticate again if expired:

~~~bash
OCI_BIN="$HOME/.cache/katcha/oci-cli-3.94.1/bin/oci"
"$OCI_BIN" session authenticate --region us-ashburn-1 --profile-name KATCHA_VAULT_ADMIN
"$OCI_BIN" session validate --profile KATCHA_VAULT_ADMIN --auth security_token
~~~

## Read-only auditing

From an accepted main SHA, without checking out branches or changing your working tree:

~~~bash
cd "$HOME/src/katcha"
git fetch origin main
RELEASE="$(git rev-parse origin/main)"
install -d -m 0700 "$HOME/.config/katcha/oci-vault-tools"
git show "$RELEASE:scripts/audit-oci-recovery-readiness.py" > "$HOME/.config/katcha/oci-vault-tools/audit-oci-recovery-readiness.py"
chmod 0600 "$HOME/.config/katcha/oci-vault-tools/audit-oci-recovery-readiness.py"
python3 "$HOME/.config/katcha/oci-vault-tools/audit-oci-recovery-readiness.py" --iam-only
~~~

With the **existing** OCI Bastion session forwarding `127.0.0.1:22022`, run the independent host check even if the administrator session expires:

~~~bash
python3 "$HOME/.config/katcha/oci-vault-tools/audit-oci-recovery-readiness.py" --backups-only
~~~

This checks Default identity-domain dynamic-resource-group metadata, the `KatchaRecovery` defined tag, the root compartment policy `katcha-recovery-vault-access` with four exact `target.secret.id` conditions, the four ACTIVE Vault secret metadata records, and whether the active primary was accidentally tagged as a replacement. It prints only pass/fail metadata, not OCIDs or credential contents.

The host phase reads timer status, mountpoint status, and filters journals for the literal success patterns emitted by `postgres-backup.sh` and `postgres-restore-test.sh`. It never prints raw journal lines. Recent success is classified as fresh if within 3 hours for the hourly backup or 8 days for the weekly restore; the service's `Result` is printed separately because it can be misleading before the first invocation.

**Any `NOT_VERIFIED`, `NO_SUCCESS_MARKER`, or `HOST_AUDIT_UNAVAILABLE` outcome requires investigation. Do not treat a successful script exit as proof all gates passed.**

Before autonomous failover, perform a controlled candidate instance-principal read of the four Vault bundles, verify R2 marker/checksum/freshness and isolated restore, and review budget and recovery switches. These are separate acceptance steps; never enable `KATCHA_OCI_RECOVERY_CONFIGURED` or paid fallback to make an audit pass.
