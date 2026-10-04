# Katcha immutable PostgreSQL disaster backups

This runbook completes the off-VM PostgreSQL/Temporal backup path for hosted Katcha.

The production media bucket and the disaster-backup bucket are intentionally different trust domains.

## Resulting layout

Normal runtime:

- media bucket: used by Katcha application services
- media credentials: mounted only into normal Katcha runtime containers

Disaster recovery:

- backup bucket: dedicated R2 bucket such as `katcha-backup-prod`
- writer credentials: Object Read & Write, scoped only to the backup bucket
- restore credentials: Object Read only, scoped only to the backup bucket
- bucket-configuration credential: Cloudflare API token that can edit R2 bucket configuration
- bucket-configuration credential is never stored on the Katcha VM

Backups are written under:

`postgres/<UTC timestamp>-<UUID>/`

Each generation contains:

- one custom-format `pg_dump` per non-template application database
- `manifest.json` with database names, sizes and SHA-256 checksums
- `_COMPLETE.json` written last

A generation is considered restorable only when the completion marker exists and all checksums verify.

## Retention policy

The supported production policy is:

- immutable retention: 30 days by default
- lifecycle expiry: 60 days by default

The lifecycle expiry must always be later than the immutable retention window.

The admin tool preserves unrelated bucket-lock and lifecycle rules when it adds or updates Katcha's rules.

## One-time Cloudflare setup

1. Create a dedicated R2 bucket, for example `katcha-backup-prod`.
2. Create one R2 S3 credential with Object Read & Write permission scoped only to that bucket.
3. Create a second R2 S3 credential with Object Read permission scoped only to that bucket.
4. Copy `deploy/backup.env.example` to a secure local file and fill in the writer credential.
5. Copy `deploy/restore.env.example` to a separate secure local file and fill in the read-only credential.
6. Create a Cloudflare API token that can edit R2 bucket configuration. Do not place this token in either file and do not store it on the Katcha VM.
7. From a trusted workstation with the repository checked out, apply the lock and lifecycle rules:

```bash
export CLOUDFLARE_ACCOUNT_ID='...'
export CLOUDFLARE_R2_ADMIN_TOKEN='...'
export KATCHA_BACKUP_R2_BUCKET='katcha-backup-prod'
export KATCHA_BACKUP_R2_PREFIX='postgres'
export KATCHA_BACKUP_RETENTION_DAYS='30'
export KATCHA_BACKUP_EXPIRATION_DAYS='60'

python -m katcha.ops.r2_bucket_lock apply
python -m katcha.ops.r2_bucket_lock check
```

After verification, remove the admin token from the shell/session.

The writer credential can technically request deletion through the S3 API, but the bucket lock denies deletion or overwrite of protected objects during the configured retention window.

## One-time OCI Vault setup

Create two OCI Vault secrets from the completed files:

- backup writer environment
- restore reader environment

Record their secret OCIDs in the GitHub Actions variables:

- `KATCHA_OCI_BACKUP_ENV_SECRET_ID`
- `KATCHA_OCI_RESTORE_ENV_SECRET_ID`

The recovery candidate bootstrap retrieves those two secrets using instance-principal authentication and stores them as:

- `/etc/katcha/backup.env`
- `/etc/katcha/restore.env`

with mode `0600`.

Do not put the Cloudflare bucket-admin token into OCI Vault for Katcha.

## Initial production-host setup

On the primary OCI host:

```bash
sudo install -d -m 0700 /etc/katcha
sudo install -m 0600 /secure/path/backup.env /etc/katcha/backup.env
sudo install -m 0600 /secure/path/restore.env /etc/katcha/restore.env

cd /opt/katcha
sudo PYTHONPATH=/opt/katcha/src python3 -m katcha.ops.disaster_recovery_validate \
  --production-env /etc/katcha/katcha.env \
  --backup-env /etc/katcha/backup.env \
  --restore-env /etc/katcha/restore.env

sudo KATCHA_REPO_ROOT=/opt/katcha \
  /bin/bash deploy/scripts/install-production-units.sh --start
```

The production supervisor also runs the credential-separation validator before starting Katcha.

## Backup schedule

`katcha-backup.timer` runs hourly with a randomized delay.

The backup runner:

1. takes an exclusive local lock so two backups cannot overlap
2. discovers every non-template database except the administrative `postgres` database
3. creates a PostgreSQL custom-format dump for each database
4. builds a checksum manifest
5. asserts that this VM still owns the active deployment epoch
6. uploads the generation with the dedicated writer credential
7. writes the completion marker last
8. removes the local staging directory only after successful upload

If upload fails, the local staging directory is retained for diagnosis.

Inspect it with:

```bash
systemctl status katcha-backup.service
journalctl -u katcha-backup.service --since '2 hours ago'
systemctl list-timers katcha-backup.timer
```

Run an immediate backup with:

```bash
sudo systemctl start katcha-backup.service
sudo journalctl -u katcha-backup.service -n 100 --no-pager
```

## Restore verification

`katcha-restore-test.timer` runs weekly.

It does not touch production PostgreSQL.

The restore verifier:

1. downloads the newest completed backup using the read-only restore credential
2. validates the completion marker and every SHA-256 checksum
3. starts a temporary PostgreSQL 16 container
4. recreates every database from its custom-format dump
5. verifies that each restored database contains application tables
6. verifies that one restored database contains a valid Katcha Alembic schema
7. destroys the temporary container and downloaded test files

Run it manually with:

```bash
sudo systemctl start katcha-restore-test.service
sudo journalctl -u katcha-restore-test.service -n 200 --no-pager
```

A successful backup upload is not considered proven disaster recovery until this restore test passes.

## Credential-separation invariants

Hosted startup fails when any of these are true:

- media and backup buckets are the same
- media and backup writer access keys are the same
- media and restore access keys are the same
- writer and restore access keys are the same
- writer and restore target different backup buckets
- writer and restore target different backup prefixes
- R2 endpoints do not use HTTPS
- R2 endpoint hostnames are not Cloudflare R2
- R2 region/path-style settings are incompatible with the hosted R2 profile

## VM recovery behavior

A replacement OCI VM restores the backup and restore environment files from Vault and enables both timers automatically.

The normal same-availability-domain failover path still moves the durable OCI block volume first.

These R2 backups are the separate disaster-recovery path for cases where the block volume cannot be reused, including a future cross-availability-domain or cross-provider restore workflow.

The current backup implementation deliberately does not pretend that cross-AD restore is already automated. It produces and continuously validates the recovery points that the cross-AD recovery slice will consume.
