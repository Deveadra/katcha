#!/usr/bin/env bash
set -euo pipefail

ROOT="${KATCHA_REPO_ROOT:-/opt/katcha}"
ENV_FILE="${KATCHA_ENV_FILE:-/etc/katcha/katcha.env}"
BACKUP_ENV_FILE="${KATCHA_BACKUP_ENV_FILE:-/etc/katcha/backup.env}"
RESTORE_ENV_FILE="${KATCHA_RESTORE_ENV_FILE:-/etc/katcha/restore.env}"
DATA_ROOT="/srv/katcha"

# Guard the complete lifetime of the attached docker compose process.
# This does NOT authorize rollout: it only rejects multiple instances of this
# supervisor after the host has explicitly adopted the revised service code.
# /run is root-owned. Keep the lease directory private so other users cannot
# replace the lock with a symlink or interfere with the primary.
lease_dir=/run/katcha-production-supervisor
if [[ -L "$lease_dir" || ( -e "$lease_dir" && ! -d "$lease_dir" ) ]]; then
  echo "Unsafe production Compose lease directory; refusing to start." >&2
  exit 23
fi
install -d -o 0 -g 0 -m 0700 "$lease_dir"
if [[ "$(stat -c '%u:%a' "$lease_dir")" != "0:700" ]]; then
  echo "Unsafe production Compose lease ownership; refusing to start." >&2
  exit 23
fi
lease_file="$lease_dir/compose.lock"
if [[ -L "$lease_file" || ( -e "$lease_file" && ! -f "$lease_file" ) ]]; then
  echo "Unsafe production Compose lock path; refusing to start." >&2
  exit 23
fi
umask 077
exec 9>>"$lease_file"
if [[ "$(stat -c '%u:%a' "$lease_file")" != "0:600" ]]; then
  echo "Unsafe production Compose lock file; refusing to start." >&2
  exit 23
fi
if ! flock --exclusive --nonblock 9; then
  echo "Another production Compose owner holds the lease; refusing duplicate startup." >&2
  exit 24
fi
# FD 9 remains open across the final exec of 'docker compose up --remove-orphans'.
# The kernel automatically releases the lease if the attached owner exits.
# No other rollout controller may invoke a competing Compose command.

cd -- "${ROOT}"

if ! mountpoint -q "${DATA_ROOT}"; then
  echo "Katcha production data volume is not mounted at ${DATA_ROOT}; refusing to start." >&2
  exit 20
fi

for directory in postgres handoff recovery backups-local; do
  if [[ ! -d "${DATA_ROOT}/${directory}" ]]; then
    echo "Missing required durable directory: ${DATA_ROOT}/${directory}" >&2
    exit 21
  fi
done

runtime_uid=10001
handoff_directories=(
  "${DATA_ROOT}/handoff"
  "${DATA_ROOT}/handoff/incoming"
  "${DATA_ROOT}/handoff/processed"
  "${DATA_ROOT}/handoff/failed"
  "${DATA_ROOT}/handoff/receipts"
)
for directory in "${handoff_directories[@]}"; do
  install -d -o "${runtime_uid}" -g "${runtime_uid}" -m 0750 "${directory}"
done

if [[ ! -r /etc/katcha/aws/config ]]; then
  echo "Missing readable hosted AWS profile at /etc/katcha/aws/config; refusing to start." >&2
  exit 22
fi

renderer_uid=10001
for directory in /etc/katcha/aws /etc/katcha/aws/runtime; do
  if [[ ! -d "$directory" || -L "$directory" ]]; then
    echo "Hosted AWS credential directory is missing or unsafe: $directory" >&2
    exit 22
  fi
  if [[ "$(stat -c '%u:%a' "$directory")" != "$renderer_uid:700" ]]; then
    echo "Hosted AWS credential directory has unsafe ownership/mode: $directory" >&2
    exit 22
  fi
done

for file in \
  /etc/katcha/aws/config \
  /etc/katcha/aws/runtime/client.pem \
  /etc/katcha/aws/runtime/client-key.pem; do
  if [[ ! -f "$file" || -L "$file" ]]; then
    echo "Hosted AWS credential file is missing or unsafe: $file" >&2
    exit 22
  fi
  if [[ "$(stat -c '%u:%a' "$file")" != "$renderer_uid:600" ]]; then
    echo "Hosted AWS credential file has unsafe ownership/mode: $file" >&2
    exit 22
  fi
done

helper=/etc/katcha/aws/aws_signing_helper
if [[ ! -f "$helper" || -L "$helper" ]]; then
  echo "Hosted AWS signing helper is missing or unsafe." >&2
  exit 22
fi
if [[ "$(stat -c '%u:%a' "$helper")" != "$renderer_uid:755" ]]; then
  echo "Hosted AWS signing helper has unsafe ownership/mode." >&2
  exit 22
fi

PYTHONPATH="${ROOT}/src${PYTHONPATH:+:${PYTHONPATH}}" \
  python3 scripts/validate_production_runtime.py --env-file "${ENV_FILE}"

PYTHONPATH="${ROOT}/src${PYTHONPATH:+:${PYTHONPATH}}" \
  python3 -m katcha.ops.disaster_recovery_validate \
    --production-env "${ENV_FILE}" \
    --backup-env "${BACKUP_ENV_FILE}" \
    --restore-env "${RESTORE_ENV_FILE}"

compose=(
  docker compose
  --project-name katcha-production
  --env-file "${ENV_FILE}"
  -f deploy/docker-compose.production.yml
)

"${compose[@]}" config --quiet
"${compose[@]}" pull

exec "${compose[@]}" up --remove-orphans
