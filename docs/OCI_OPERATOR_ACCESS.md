# Operator OCI access — one short command

Run in Ubuntu WSL from the Katcha checkout:

```bash
cd ~/src/katcha
bash scripts/katcha-oci-admin.sh status
bash scripts/katcha-oci-admin.sh connect
bash scripts/katcha-oci-admin.sh audit
bash scripts/katcha-oci-admin.sh doctor
bash scripts/katcha-oci-admin.sh repair-access    # explicit approval to update ONE Bastion /32
bash scripts/katcha-oci-admin.sh disconnect
bash scripts/katcha-oci-admin.sh reauth
```

- `status`: read-only admin token validity, managed port-forward tunnel health, GitHub recovery safety switches.
- `connect`: reuse an existing **managed and verified** SSH tunnel, or create **one temporary OCI Bastion port-forwarding session on the existing Bastion**, at localhost port 22022 (3-hour session lifetime). Before creating a session, it attempts a read-only Bastion CIDR check and refuses confirmed mismatches; if external IP verification is unavailable, it warns without incorrectly claiming the allowlist matches.
- `audit`: run the existing independent read-only IAM/Vault, capacity and production-host backup/restore audits. Missing or stale audit evidence fails the overall status even when an underlying audit exits 0.
- `doctor`: inspect access and compare the existing OCI Bastion client CIDR allowlist with the current public IPv4 observed via HTTPS. It prints only ALLOWED/MISMATCH/UNVERIFIED and changes nothing. Under split-tunnel VPNs, HTTPS egress may differ from SSH egress; confirm before modifying OCI settings.
- `repair-access`: **authorized explicit mutation** of the existing OCI Bastion client-IP allowlist, and nothing else. It confirms the Bastion is ACTIVE and has exactly one IPv4 `/32` rule, verifies your current public IPv4 using two independent HTTPS sources, prints the old and proposed `/32`, and requires typing `UPDATE`. If already correct, it does not mutate. After exactly one update it polls the OCI control plane to verify the result. Unexpected rules, changed IPs, changed policies, noninteractive execution, missing login, and concurrent listener use stop safely. If the update status is uncertain, run `doctor`; do not retry blindly.
- `disconnect`: close only this wrapper's managed SSH process, delete its own OCI Bastion session and ephemeral RSA key. Refuse to kill an unknown listener.
- `reauth`: open interactive OCI administrator browser login only if the `KATCHA_VAULT_ADMIN` token is invalid. Login output and input use your terminal directly, so follow the displayed browser/MFA instructions. It stops after five minutes if the login does not finish; Ctrl+C exits cleanly. Bastion connect uses the separate recovery API-key config; it **does not need** admin browser reauthentication.

Prerequisites: WSL Linux, installed isolated OCI CLI at `~/.cache/katcha/oci-cli-3.94.1/bin/oci`, working `gh` login for `Deveadra/katcha`, existing recovery API-key config `~/.config/katcha/oci-github-recovery/config`, local `~/.ssh/katcha-oci` and **pinned primary host key** at `~/.config/katcha/production/bastion/target_known_hosts`. If the latter is absent, stop and re-establish it via a trusted verification path; do not disable strict host-key checks.

The wrapper's state, ephemeral Bastion RSA key, logs and persistent Bastion server known-hosts live under `~/.config/katcha/production/bastion/operator-tunnel/` with owner-only permissions. The SSH process is detached from the command's terminal but only remains alive while WSL is running and OCI's temporary session remains active. The wrapper refuses to overwrite or disconnect unmanaged port 22022 listeners and does not print command stderr or credentials. If a new session is created but SSH fails, it attempts to delete only that session. If deletion fails, state is preserved for review.

**Boundaries:** None of these actions creates a compute instance, volume, Vault secret, IAM policy, new Bastion, Cloudflare leader, or deployment. No production disk or recovery switch is touched. Creating a Bastion **session** is still an OCI API operation; it is initiated **only** by `connect`. Launching an E2 Micro identity probe remains separately gated: the last audit showed 50 GB of remaining storage, all of which a test instance would consume.

Old `bootstrap-acquired-oci-host.sh` also creates Bastion sessions but additionally provisions/changes production infrastructure; **do not use that bootstrap script for routine operator access**.

## Bastion 'closed by remote host' failure

Oracle's official Bastion troubleshooting page identifies the error `Connection to host.bastion... closed by remote host.` as a possible client-IP allowlist mismatch. The original production bootstrap restricted the Bastion to the operator's then-current public IPv4 (/32). If the operator's IPv4 changed, `doctor` can flag the mismatch without creating another session. Confirm the actual SSH egress IP (especially with a VPN); updating the Bastion allowlist is a **separate explicit operator action** and requires a new Bastion session afterward. Do not rerun the initial production bootstrap or broaden the allowlist indiscriminately.


## Authorized client IP repair (ISP address changed)

When `doctor` reports `bastion_client_allowlist: MISMATCH` and your Windows/WSL network is not on a VPN or proxy, run the following **from a trusted interactive WSL terminal**:

```bash
cd ~/src/katcha
bash scripts/katcha-oci-admin.sh repair-access
```

The script prints the existing and newly detected public IPv4 `/32` CIDRs *only in your local terminal*. Check that the proposed address is your actual ISP public IP, then type `UPDATE`. The operation updates only the existing Bastion's `client-cidr-list` with one IPv4 `/32` rule. It does not update a network security group, VCN, compute instance, IAM policy, production disk, recovery control gate, or Vault. A completed update must be verified before proceeding. When completed, run:

```bash
bash scripts/katcha-oci-admin.sh connect
bash scripts/katcha-oci-admin.sh audit
```

If your observed HTTPS and SSH egress addresses differ, stop; this repair does not infer SSH egress from a VPN, split tunnel, or proxy. This command deliberately replaces only an existing *single* `/32` rule and refuses unfamiliar allowlist layouts, rather than silently removing additional permitted operators. For an unexpected OCI error or ambiguous update result, inspect via `doctor` before retrying; **OCI access changes are never attempted during `status`, `doctor`, or `audit`.**
