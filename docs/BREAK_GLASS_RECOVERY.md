# Katcha off-OCI break-glass recovery

This runbook establishes the disaster secret path used when OCI Vault cannot bootstrap a replacement Katcha control plane.

Normal recovery remains OCI Vault. Break-glass recovery is manual and must be selected explicitly from GitHub Actions.

## Security model

- OCI Vault remains the normal production secret authority.
- A dedicated Cloudflare R2 bucket stores the off-OCI escrow.
- The durable escrow object is encrypted client-side with Fernet.
- The escrow key is stored in GitHub Actions Secrets and in an operator-controlled offline/password-manager copy.
- GitHub Actions holds a dedicated R2 credential scoped only to the break-glass bucket.
- Normal Katcha containers and the normal OCI identity never receive the escrow key or break-glass R2 credential.
- A manual recovery rewraps the durable escrow into a fresh one-time encrypted object.
- The one-time object is served through a short-lived presigned URL and deleted after recovery/drill even on failure.
- A Cloudflare lifecycle rule removes stale handoffs within 24 hours if workflow cleanup is interrupted.
- The durable escrow uses a different prefix and is not covered by that lifecycle.

The one-time key and presigned URL are temporarily embedded in OCI recovery user-data. They are not long-lived production credentials: the URL expires, the handoff object is deleted, and the key decrypts only that handoff.

## Required R2 layout

Use a dedicated bucket, for example katcha-break-glass-prod. Do not use the normal media bucket or immutable database-backup bucket.

Recommended prefixes:

- durable escrow: escrow/
- short-lived handoffs: bootstrap-handoff/

Create an R2 S3 credential with Object Read and Write permission scoped only to this bucket. It is for GitHub Actions and trusted recovery workstations only.

Create a separate Cloudflare API token capable of editing R2 lifecycle rules. Use that token only from a trusted workstation. Do not put it on the OCI VM or into Katcha runtime.

## GitHub repository variables

Configure:

- KATCHA_BREAK_GLASS_R2_ENDPOINT_URL = https://<account-id>.r2.cloudflarestorage.com
- KATCHA_BREAK_GLASS_R2_BUCKET = dedicated break-glass bucket
- KATCHA_BREAK_GLASS_R2_PREFIX = bootstrap-handoff
- KATCHA_BREAK_GLASS_ESCROW_PREFIX = escrow
- KATCHA_BREAK_GLASS_R2_REGION = auto
- KATCHA_BREAK_GLASS_R2_FORCE_PATH_STYLE = false
- KATCHA_BREAK_GLASS_HANDOFF_TTL_SECONDS = 1800
- KATCHA_BREAK_GLASS_ESCROW_OBJECT_KEY = leave unset until first escrow publish succeeds

## GitHub Actions secrets

Configure only:

- KATCHA_BREAK_GLASS_R2_ACCESS_KEY
- KATCHA_BREAK_GLASS_R2_SECRET_KEY
- KATCHA_BREAK_GLASS_ESCROW_KEY

Do not put production environment files, database/provider credentials, or the AWS bundle directly into GitHub Secrets.

## Generate the escrow key

From a trusted workstation with the repository checked out:

    cd ~/src/katcha
    python -m pip install -e .
    umask 077
    python -m katcha.ops.break_glass generate-key > /tmp/katcha-break-glass.key

Store the single line in /tmp/katcha-break-glass.key in both GitHub Actions secret KATCHA_BREAK_GLASS_ESCROW_KEY and an operator-controlled password manager/offline recovery record. Never commit it.

## Configure stale-handoff deletion

From a trusted workstation:

    cd ~/src/katcha
    export CLOUDFLARE_ACCOUNT_ID='<account-id>'
    export CLOUDFLARE_R2_ADMIN_TOKEN='<bucket-configuration-token>'
    export KATCHA_BREAK_GLASS_R2_BUCKET='katcha-break-glass-prod'
    export KATCHA_BREAK_GLASS_R2_PREFIX='bootstrap-handoff'
    export KATCHA_BREAK_GLASS_R2_EXPIRATION_SECONDS='86400'
    python -m katcha.ops.break_glass_lifecycle apply
    python -m katcha.ops.break_glass_lifecycle check

The command preserves unrelated lifecycle rules. The lifecycle must target bootstrap-handoff/ only and must never target escrow/. Remove the Cloudflare admin token from the shell after setup.

## Publish the initial durable escrow

The escrow contains exactly four logical artifacts:

1. production Katcha environment
2. backup-writer environment
3. restore-reader environment
4. AWS bootstrap directory as a tar.gz

On a trusted host with the current production files:

    umask 077
    tar -C /etc/katcha/aws -czf /tmp/katcha-break-glass-aws.tgz .

    export KATCHA_BREAK_GLASS_R2_ENDPOINT_URL='https://<account-id>.r2.cloudflarestorage.com'
    export KATCHA_BREAK_GLASS_R2_ACCESS_KEY='<scoped-break-glass-access-key>'
    export KATCHA_BREAK_GLASS_R2_SECRET_KEY='<scoped-break-glass-secret-key>'
    export KATCHA_BREAK_GLASS_R2_BUCKET='katcha-break-glass-prod'
    export KATCHA_BREAK_GLASS_R2_PREFIX='bootstrap-handoff'
    export KATCHA_BREAK_GLASS_ESCROW_PREFIX='escrow'
    export KATCHA_BREAK_GLASS_R2_REGION='auto'
    export KATCHA_BREAK_GLASS_R2_FORCE_PATH_STYLE='false'
    export KATCHA_BREAK_GLASS_ESCROW_KEY="$(cat /tmp/katcha-break-glass.key)"

    python -m katcha.ops.break_glass publish-escrow \
      --production-env /etc/katcha/katcha.env \
      --backup-env /etc/katcha/backup.env \
      --restore-env /etc/katcha/restore.env \
      --aws-bundle /tmp/katcha-break-glass-aws.tgz

The command prints JSON with an object_key such as escrow/20261004T023000Z-<random>.bin. Only ciphertext is uploaded.

Set GitHub repository variable KATCHA_BREAK_GLASS_ESCROW_OBJECT_KEY to that exact object key.

After the key is safely stored elsewhere:

    rm -f /tmp/katcha-break-glass-aws.tgz
    rm -f /tmp/katcha-break-glass.key
    unset KATCHA_BREAK_GLASS_ESCROW_KEY
    unset KATCHA_BREAK_GLASS_R2_SECRET_KEY

## Validate without touching OCI

Run GitHub Actions workflow Break-glass escrow drill.

The drill:

1. reads the durable encrypted escrow from R2
2. decrypts it only on the GitHub runner
3. verifies every file checksum
4. re-encrypts it with a fresh one-time key
5. uploads a short-lived handoff
6. downloads the handoff through its presigned URL
7. installs it into a fresh temporary directory
8. validates the Katcha production environment
9. validates backup/restore credential separation
10. verifies the AWS bootstrap directory and rejects symlinks
11. deletes the one-time handoff in an always-run cleanup step

The drill does not create, stop, or modify OCI compute. Do not consider the escrow ready until this workflow succeeds.

## Run a live Vault-independent recovery

Run GitHub Actions workflow OCI production recovery and provide:

- incident_id
- expected_active_epoch
- active_deployment_id
- secret_source = break-glass

The incident, epoch, and deployment values must match the current recovery coordinator state. Stale values are rejected before compute recovery.

The workflow then rewraps the durable escrow into a 30-minute handoff, masks the one-time URL/key, runs the normal bounded OCI recovery state machine, installs recovered secrets on the candidate without calling OCI Vault, proves storage/local/public/fence readiness, commits authority, and deletes the handoff.

## Rotation

Republish after any escrowed material changes, including production credentials, the persistent credential-encryption key, tunnel/fence credentials, backup/restore credentials, AWS Roles Anywhere material, or provider/OAuth credentials.

Use publish-then-switch:

1. publish a new escrow object
2. leave KATCHA_BREAK_GLASS_ESCROW_OBJECT_KEY on the old object
3. record the new object key
4. change KATCHA_BREAK_GLASS_ESCROW_OBJECT_KEY to the new object
5. run Break-glass escrow drill
6. only after success, optionally delete the old escrow

Delete a superseded escrow only from a trusted workstation:

    python -m katcha.ops.break_glass delete-escrow \
      --object-key 'escrow/<old-object>.bin'

Never delete the currently configured escrow object.

## Guarantees and limits

This removes OCI Vault as a required dependency for secret restoration and keeps the durable recovery material outside OCI.

It does not by itself make OCI tenancy or regional compute available. The current compute recovery backend still targets OCI. The escrow format is provider-neutral so a future non-OCI control-plane backend can consume the same material.

Full production acceptance still requires one controlled live recovery with secret_source=break-glass and evidence that the candidate reaches readiness and leadership without a successful OCI Vault secret retrieval.
