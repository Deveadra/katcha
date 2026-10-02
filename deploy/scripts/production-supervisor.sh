#!/usr/bin/env bash
set -euo pipefail

ROOT="${KATCHA_REPO_ROOT:-/opt/katcha}"
ENV_FILE="${KATCHA_ENV_FILE:-/etc/katcha/katcha.env}"
DATA_ROOT="/srv/katcha"

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

python3 scripts/validate_production_runtime.py --env-file "${ENV_FILE}"

compose=(
  docker compose
  --project-name katcha-production
  --env-file "${ENV_FILE}"
  -f deploy/docker-compose.production.yml
)

if [[ -n "${KATCHA_AWS_SIGNING_HELPER_PATH:-}" ]]; then
  compose+=(-f docker-compose.aws-roles-anywhere.yml)
fi

"${compose[@]}" config --quiet
"${compose[@]}" pull

exec "${compose[@]}" up --remove-orphans
