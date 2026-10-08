# OCI Vault recovery secrets — guarded automated provisioning

The four OCI recovery secrets are created automatically. Never paste production environment files or Roles Anywhere private keys into the Oracle Console, GitHub Secrets, terminal logs, or chats.

This is distinct from the restricted GitHub recovery API user. A Vault operator uses a temporary administrator security-token session to create secrets, and replacement VMs retrieve them with an instance principal. See PR #335 and docs/LIVE_CLOUD_CUTOVER.md for the IAM split.

## 1. Administrator session

The operator has already created a standard Vault with a SOFTWARE AES symmetric key in katcha-prod, us-ashburn-1. Default names are katcha-recovery-vault and katcha-recovery-secrets-key. The tool supports --vault-name and --key-name overrides and refuses ambiguous names, HSM keys, or virtual private vaults.

Use the isolated CLI from the OCI bootstrap, not the restricted GitHub service account or Katcha application .venv:

~~~bash
OCI_BIN="$HOME/.cache/katcha/oci-cli-3.94.1/bin/oci"
"$OCI_BIN" session authenticate --region us-ashburn-1 --profile-name KATCHA_VAULT_ADMIN
"$OCI_BIN" session validate --profile KATCHA_VAULT_ADMIN --auth security_token
~~~

Authenticate in your browser. Do not paste session tokens into ChatGPT, GitHub, or shell history.

## 2. Extract exact reviewed scripts without changing local history

~~~bash
cd "$HOME/src/katcha"
git fetch origin main
RELEASE="$(git rev-parse origin/main)"
mkdir -p /tmp/katcha-vault-provisioner
chmod 0700 /tmp/katcha-vault-provisioner
git show "$RELEASE:scripts/provision-oci-recovery-vault.py" > /tmp/katcha-vault-provisioner/provision-oci-recovery-vault.py
git show "$RELEASE:scripts/pack-oci-aws-vault-credentials.py" > /tmp/katcha-vault-provisioner/pack-oci-aws-vault-credentials.py
python3 /tmp/katcha-vault-provisioner/provision-oci-recovery-vault.py
~~~

Inspect-only prints Vault/key metadata and existing secret states. No protected host credentials are read or uploaded. After inspection succeeds, ensure the OCI Bastion forward is active on 127.0.0.1:22022 and run:

~~~bash
python3 /tmp/katcha-vault-provisioner/provision-oci-recovery-vault.py --expected-epoch 4 --apply
~~~

Epoch 4 is the last confirmed original production leader. Stop and reconcile if current coordinator authority has changed; do not assume an older epoch remains valid.

## 3. What --apply does

1. Confirms the operator's short-lived OCI administrator session; rejects already enabled recovery/paid flags.
2. Resolves exactly one ACTIVE default Vault and its ENABLED SOFTWARE AES key in the configured production compartment.
3. Reads four current protected host sources via the existing authenticated Bastion SSH connection; the OCI host is not modified or restarted.
4. Uses the PR #336 credential-only packer to produce the three-file AWS archive: config, runtime/client.pem, and runtime/client-key.pem. The 4 MB public AWS signing helper is explicitly excluded.
5. Creates each secret via the OCI CLI with --from-json pointing to a temporary protected file. Base64 secret content is never supplied as command-line arguments or printed.
6. Refuses to overwrite existing secrets; only byte-for-byte matching CURRENT content may be reused on retries.
7. Waits for all four Vault secrets to become ACTIVE, then compares retrieved CURRENT content to the staged source using hashes, without displaying secrets.
8. Registers only real ocid1.vaultsecret... OCIDs in the four GitHub Actions Variables after all checks pass.
9. Deletes staged plaintext/key files on completion or exceptions.

If interrupted, rerun the same command. Matching existing Vault secrets are reused, not rotated. Conflicting or inaccessible secrets stop the process.

## 4. Gates that remain

The script intentionally leaves KATCHA_OCI_RECOVERY_CONFIGURED=false and the watchdog and paid fallback disabled. Before enabling autonomous recovery, verify replacement candidate instance-principal retrieval, tight dynamic-group membership, AWS helper download/hash on the actual recovery network, real R2 backup plus isolated restore results, and GitHub recovery permissions. Paid compute must remain disabled until independently authorized under the budget ledger.

See docs/LIVE_CLOUD_CUTOVER.md and docs/OCI_RECOVERY_AUTOMATION.md.
