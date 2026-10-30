# Acquisition worker credentials

Two independent credentials are required: Cloudflare Access permits reaching the
API; the Katcha worker token authorizes acquisition work for selected channels.
Neither credential replaces the other. Keep values out of chat, Git, command-line
arguments, environment variables, logs, and the core environment file.

## Responsibilities

| Responsibility | Who handles it |
| --- | --- |
| Create service credentials | You, in Cloudflare |
| Install secrets on the OCI host | Authorized deployment operator |
| Configure and verify worker secret mounts | AI agent and deployment operator |

The human operator creates `katcha-acquisition-vm1` in Cloudflare and includes only
that token in a Service Auth policy for the intended application, preserving
interactive access. The deployment operator separately prepares a Katcha principal
with `acquisition:work`, scoped to the actual Forescene channel UUID, while preserving
the existing operator principal. Never enable a worker-only principal registry that
would revoke operator access.

## Install or check credentials

Run the reviewed `scripts/acquisition-credentials.py` on the OCI host from a trusted
checkout as the authorized deployment operator. Python 3 and local Docker are
required. The script never starts the acquisition worker, pulls images, changes
Cloudflare policies, or generates Katcha tokens.

First obtain the immutable ID of an already-local Python image:

```bash
sudo docker inspect --format '{{.Image}}' katcha-production-api-1
```

Use the returned `sha256:...` value below. It is an image identity, not a secret.
For **already installed Cloudflare files**, run:

```bash
sudo python3 scripts/acquisition-credentials.py --check-only --kind cloudflare --image sha256:REPLACE_WITH_IMAGE_ID
```

For a **new installation**, omit `--check-only`. It asks for each value twice with
hidden terminal input. It rejects empty values, whitespace, existing files, symlinks,
and unsafe directory ancestors. It installs only the selected credential type:

```bash
sudo python3 scripts/acquisition-credentials.py --kind cloudflare --image sha256:REPLACE_WITH_IMAGE_ID
```

Once the separate scoped Katcha token has been prepared, the operator can install it:

```bash
sudo python3 scripts/acquisition-credentials.py --kind worker --image sha256:REPLACE_WITH_IMAGE_ID
sudo python3 scripts/acquisition-credentials.py --check-only --kind all --image sha256:REPLACE_WITH_IMAGE_ID
```

Default host directory: `/etc/katcha-acquisition/credentials`, owned by
`10001:10001`, mode `0700`. Files must be regular, nonempty, single-link files owned
by `10001:10001`, mode `0600`. Use `--directory` for another trusted absolute path.
The installer refuses overwrites; rotation requires a separately supervised
replacement and worker recreation because file bind mounts retain the old inode.
A failed container check may leave successfully installed files in place: correct
the reported prerequisite and rerun `--check-only`, rather than reinstalling.
Do not install credentials while an active worker is consuming that directory.

The checker starts a temporary container as UID/GID 10001, overrides its entrypoint,
disables networking, drops capabilities, and mounts only the selected files read-only.
It checks metadata, opens and closes each file **without reading any bytes**, and
checks the filesystem read-only flag. It removes only its temporary checker container.
This verifies file access and mount behavior, not authentication with Cloudflare or Katcha. Run the live read-only `python -m katcha.acquisition.worker --check-auth` check from the actual worker runtime after mounting credentials; see [Access authentication and 403 diagnosis](ACQUISITION_ACCESS_AUTH.md).

## Configure the worker

Set `KATCHA_ACQUISITION_CREDENTIAL_DIR=/etc/katcha-acquisition/credentials` in the
supervisor's Compose configuration. The single-host overlay explicitly binds:

| Host file | Container file |
| --- | --- |
| `cf-access-id` | `/run/acquisition/cf-access-id` |
| `cf-access-secret` | `/run/acquisition/cf-access-secret` |
| `worker-token` | `/run/acquisition/worker-token` |

All three mounts are read-only; missing sources fail instead of creating directories.
The overlay sets the worker's matching credential-file environment variables.
The dedicated worker configuration must also set its trusted API URL and channel UUID.
Validate the combined configuration privately with `docker compose ... config --quiet`.
Do not use `up` to test mounts; use the isolated checker above.

## VM1 status and activation gates

On 2026-10-23, the existing Cloudflare files passed host metadata checks and the
isolated UID 10001 read-only mount check. No credential contents were read. The
Katcha worker token remains outstanding. The overlay changes are prepared source,
not deployed configuration. Cloudflare/API authentication is still unverified.

Keep the worker inactive until the scoped worker principal/token, authentication
checks, exact-release CI/image gates, migration readiness, backup/restore checks,
workflow drain/continuity checks, and supervised activation prerequisites in
[SINGLE_HOST_OPERATION.md](SINGLE_HOST_OPERATION.md) are satisfied. Creation of a
Cloudflare token alone is not authorization to activate this worker.
