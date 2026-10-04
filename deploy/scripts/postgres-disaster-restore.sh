#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="${KATCHA_REPO_ROOT:-/opt/katcha}"
ENV_FILE="${KATCHA_ENV_FILE:-/etc/katcha/katcha.env}"
RESTORE_ENV_FILE="${KATCHA_RESTORE_ENV_FILE:-/etc/katcha/restore.env}"
COMPOSE_FILE="${KATCHA_PRODUCTION_COMPOSE_FILE:-$REPO_ROOT/deploy/docker-compose.production.yml}"
DATA_ROOT="${KATCHA_DATA_ROOT:-/srv/katcha}"
POSTGRES_ROOT="$DATA_ROOT/postgres"
RESTORE_ROOT="$DATA_ROOT/backups-local/cross-ad-restore"
MAX_BACKUP_AGE_SECONDS="${KATCHA_CROSS_AD_MAX_BACKUP_AGE_SECONDS:-7200}"

exec 9>/run/katcha-postgres-disaster-restore.lock
if ! flock -n 9; then
  echo "Katcha disaster restore is already running; refusing a second restore." >&2
  exit 40
fi

if [[ ! "$MAX_BACKUP_AGE_SECONDS" =~ ^[0-9]+$ || "$MAX_BACKUP_AGE_SECONDS" -lt 1 ]]; then
  echo "KATCHA_CROSS_AD_MAX_BACKUP_AGE_SECONDS must be a positive integer." >&2
  exit 41
fi

mkdir -p "$POSTGRES_ROOT" "$RESTORE_ROOT"
if find "$POSTGRES_ROOT" -mindepth 1 -maxdepth 1 -print -quit | grep -q .; then
  echo "PostgreSQL data directory is not empty; refusing destructive disaster restore." >&2
  exit 42
fi

compose=(
  docker compose
  --project-name katcha-production
  --env-file "$ENV_FILE"
  -f "$COMPOSE_FILE"
)

cleanup_download() {
  rm -rf "$RESTORE_ROOT"
}
trap cleanup_download EXIT

"${compose[@]}" pull postgres
"${compose[@]}" up -d postgres

for attempt in $(seq 1 90); do
  if "${compose[@]}" exec -T postgres sh -lc \
      'pg_isready -U "$POSTGRES_USER" -d postgres' >/dev/null 2>&1; then
    break
  fi
  if [[ "$attempt" -eq 90 ]]; then
    "${compose[@]}" logs --no-color postgres >&2 || true
    echo "Fresh PostgreSQL did not become ready for disaster restore." >&2
    exit 43
  fi
  sleep 1
done

rm -rf "$RESTORE_ROOT"
mkdir -p "$RESTORE_ROOT"

"${compose[@]}" --profile ops run --rm --no-deps backup-reader \
  python -m katcha.ops.disaster_backup download-latest \
  --destination /backup/cross-ad-restore \
  --max-age-seconds "$MAX_BACKUP_AGE_SECONDS"

backup_dir="$(find "$RESTORE_ROOT" -mindepth 1 -maxdepth 1 -type d -print | sort | tail -n 1)"
if [[ -z "$backup_dir" || ! -f "$backup_dir/manifest.json" ]]; then
  echo "Disaster restore could not locate the downloaded backup." >&2
  exit 44
fi

plan="$RESTORE_ROOT/restore-plan.tsv"
"${compose[@]}" --profile ops run --rm --no-deps backup-reader \
  python -m katcha.ops.disaster_backup restore-plan \
  --directory "/backup/cross-ad-restore/$(basename "$backup_dir")" > "$plan"

test -s "$plan"

restored=0
alembic_seen=0
while IFS=$'\t' read -r filename database; do
  [[ -n "$filename" && -n "$database" ]] || continue

  echo "Restoring PostgreSQL database: $database"
  "${compose[@]}" exec -T postgres sh -c \
    'dropdb -U "$POSTGRES_USER" --if-exists --force "$1" && createdb -U "$POSTGRES_USER" "$1"' \
    sh "$database"

  docker cp "$backup_dir/$filename" "katcha-production-postgres-1:/tmp/$filename" >/dev/null
  "${compose[@]}" exec -T postgres sh -c \
    'pg_restore -U "$POSTGRES_USER" --exit-on-error --no-owner --no-acl -d "$1" "$2"' \
    sh "$database" "/tmp/$filename"
  "${compose[@]}" exec -T postgres rm -f "/tmp/$filename"

  table_count="$(
    "${compose[@]}" exec -T postgres sh -c \
      'psql -U "$POSTGRES_USER" -d "$1" -At -v ON_ERROR_STOP=1 -c "SELECT count(*) FROM pg_catalog.pg_tables WHERE schemaname NOT IN ('"'"'pg_catalog'"'"','"'"'information_schema'"'"');"' \
      sh "$database"
  )"
  if [[ ! "$table_count" =~ ^[0-9]+$ || "$table_count" -lt 1 ]]; then
    echo "Restored database $database contains no application tables." >&2
    exit 45
  fi

  has_alembic="$(
    "${compose[@]}" exec -T postgres sh -c \
      'psql -U "$POSTGRES_USER" -d "$1" -At -v ON_ERROR_STOP=1 -c "SELECT CASE WHEN to_regclass('"'"'public.alembic_version'"'"') IS NULL THEN 0 ELSE 1 END;"' \
      sh "$database"
  )"
  if [[ "$has_alembic" == "1" ]]; then
    alembic_seen=1
  fi
  restored=$((restored + 1))
done < "$plan"

if [[ "$restored" -lt 1 ]]; then
  echo "Disaster restore did not restore any databases." >&2
  exit 46
fi
if [[ "$alembic_seen" -ne 1 ]]; then
  echo "Disaster restore did not recover the Katcha Alembic schema." >&2
  exit 47
fi

rm -rf "$RESTORE_ROOT"
trap - EXIT
echo "Katcha disaster restore completed for $restored databases."
