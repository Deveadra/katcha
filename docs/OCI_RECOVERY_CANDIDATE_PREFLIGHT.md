# OCI recovery candidate capacity and identity-probe preflight

After production epoch 4, four Vault secret metadata checks, restricted IAM and the isolated R2 restore of three PostgreSQL databases passed, the remaining question is whether a **live tagged instance principal** can retrieve the bundles without affecting production.

## October 2026 OCI Always Free limits

OCI documents the revised home-region entitlements as:

- **A1 Flex**: 2 OCPU + 12 GiB equivalent for Always Free tenancies (1,500 OCPU hours and 9,000 GB hours per month). The active Katcha primary already reserves these resources. An additional A1 launch while it runs must **not** be assumed free.
- **E2.1.Micro**: up to two separate AMD micro instances, limited to a supported availability domain.
- **Boot + block storage**: 200 GB combined in the home region, including all boot volumes; assume at least **50 GB** for a new test boot volume.
- Shape listing does **not** prove usable capacity. A free entitlement is **not** a billing guarantee; verify account tier, quotas, and the Always Free eligibility label in the OCI Console before any actual launch.

Official source: https://docs.oracle.com/en-us/iaas/Content/FreeTier/freetier_topic-Always_Free_Resources.htm

## Read-only capacity inventory

The script below **does not create, stop, start, tag, detach, mount or delete anything**. It uses the existing authenticated `KATCHA_VAULT_ADMIN` token and repository **variables** to:

1. Confirm the OCI region is the tenancy home region.
2. List all root + descendant compartments and their running/stopped instances.
3. Count allocated A1 OCPUs/RAM and E2.1.Micro slots **across compartments**. STOPPED instances still count.
4. Enumerate all boot and block volumes in each compartment and availability domain. AVAILABLE but unattached volumes still count toward the storage estimate.
5. Determine which domains list the E2 Micro shape. **This is not capacity reservation or billing proof.**
6. Confirm the real production primary is RUNNING and all three recovery safety switches remain `false`.

Install the **audited merged release** into the existing protected tool folder; do not switch or rewrite the current checkout:

```bash
cd "$HOME/src/katcha"
git fetch origin main
RELEASE="$(git rev-parse origin/main)"
install -d -m 0700 "$HOME/.config/katcha/oci-vault-tools"
umask 077
git show "$RELEASE:scripts/audit-oci-recovery-readiness.py" > "$HOME/.config/katcha/oci-vault-tools/audit-oci-recovery-readiness.py"
git show "$RELEASE:scripts/audit-oci-free-candidate-capacity.py" > "$HOME/.config/katcha/oci-vault-tools/audit-oci-free-candidate-capacity.py"
python3 "$HOME/.config/katcha/oci-vault-tools/audit-oci-free-candidate-capacity.py"
```

If your OCI session expires, reauthenticate `KATCHA_VAULT_ADMIN` separately. The OCI administrator security-token login is **not** the OCI Bastion SSH tunnel. Refreshing Bastion does not extend OCI API authentication. Check and refresh the session before running the inventory:

```bash
OCI_BIN="$HOME/.cache/katcha/oci-cli-3.94.1/bin/oci"
"$OCI_BIN" session validate --profile KATCHA_VAULT_ADMIN --auth security_token
# Only if invalid or expired:
"$OCI_BIN" session authenticate --region us-ashburn-1 --profile-name KATCHA_VAULT_ADMIN
```

`OCI_ADMIN_SESSION_UNAVAILABLE` indicates the security-token check failed; `OCI_READ_FAILED at compute instance list` (or another safe request category) indicates a failure reading that specific metadata resource. Both stop before any mutation. These errors never contain secret material or provider stderr.

All output is a capacity **snapshot**, not an OCI spend forecast. A failed compartment/instance/volume/region query must stop the inventory rather than undercount usage or tell you an unsafe launch is available.

**Never set** `KATCHA_OCI_RECOVERY_CONFIGURED`, `KATCHA_EXTERNAL_COMPUTE_ENABLED` or `KATCHA_OCI_PAID_FALLBACK_ENABLED` to make the preflight pass.

## Next decision

- `E2_micro_identity_probe_eligibility: BLOCKED`: do not launch another VM; design a scheduled disruptive ARM-only recovery game-day after backups and rollback are prepared, or choose a **separately approved**, bounded paid test.
- `POSSIBLE_NOT_BILLING_GUARANTEED`: review OCI Console account/billing/Always Free label and AD network/private egress constraints; then separately approve a short-lived **E2 Micro identity-only probe**. Do not attach Katcha's persistent block volume, mount production filesystems, run production cloud-init, create a leadership candidate, or call the recovery dispatcher.
- The approved micro probe will test negative (untagged) authorization and positive tagged instance-principal access for the exact four bundles, reporting only status and content lengths; it is **not** the A1 production bootstrap test, nor proof of successful cross-AD cutover. Explicitly verify cleanup of probe VM and boot volume afterward.

Evidence already held: backups and isolated PostgreSQL restore for three databases successful; an OCI instance principal has **not** yet been tested. Autonomous recovery stays disabled until additional acceptance.
