# Operator OCI access — one short command

Run in Ubuntu WSL from the Katcha checkout:

```bash
cd ~/src/katcha
bash scripts/katcha-oci-admin.sh status
bash scripts/katcha-oci-admin.sh connect
bash scripts/katcha-oci-admin.sh audit
bash scripts/katcha-oci-admin.sh doctor
bash scripts/katcha-oci-admin.sh disconnect
bash scripts/katcha-oci-admin.sh reauth
```

- `status`: read-only admin token validity, managed port-forward tunnel health, GitHub recovery safety switches.
- `connect`: reuse an existing **managed and verified** SSH tunnel, or create **one temporary OCI Bastion port-forwarding session on the existing Bastion**, at localhost port 22022 (3-hour session lifetime).
- `audit`: run the existing independent read-only IAM/Vault, capacity and production-host backup/restore audits. Missing or stale audit evidence fails the overall status even when an underlying audit exits 0.
- `doctor`: inspect and suggest next actions without renewing credentials.
- `disconnect`: close only this wrapper's managed SSH process, delete its own OCI Bastion session and ephemeral RSA key. Refuse to kill an unknown listener.
- `reauth`: open interactive OCI administrator browser login only if the `KATCHA_VAULT_ADMIN` token is invalid. Bastion connect uses the separate recovery API-key config; it **does not need** admin browser reauthentication.

Prerequisites: WSL Linux, installed isolated OCI CLI at `~/.cache/katcha/oci-cli-3.94.1/bin/oci`, working `gh` login for `Deveadra/katcha`, existing recovery API-key config `~/.config/katcha/oci-github-recovery/config`, local `~/.ssh/katcha-oci` and **pinned primary host key** at `~/.config/katcha/production/bastion/target_known_hosts`. If the latter is absent, stop and re-establish it via a trusted verification path; do not disable strict host-key checks.

The wrapper's state, ephemeral Bastion RSA key, logs and persistent Bastion server known-hosts live under `~/.config/katcha/production/bastion/operator-tunnel/` with owner-only permissions. The SSH process is detached from the command's terminal but only remains alive while WSL is running and OCI's temporary session remains active. The wrapper refuses to overwrite or disconnect unmanaged port 22022 listeners and does not print command stderr or credentials. If a new session is created but SSH fails, it attempts to delete only that session. If deletion fails, state is preserved for review.

**Boundaries:** None of these actions creates a compute instance, volume, Vault secret, IAM policy, new Bastion, Cloudflare leader, or deployment. No production disk or recovery switch is touched. Creating a Bastion **session** is still an OCI API operation; it is initiated **only** by `connect`. Launching an E2 Micro identity probe remains separately gated: the last audit showed 50 GB of remaining storage, all of which a test instance would consume.

Old `bootstrap-acquired-oci-host.sh` also creates Bastion sessions but additionally provisions/changes production infrastructure; **do not use that bootstrap script for routine operator access**.
