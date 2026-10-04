#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="${KATCHA_REPO_ROOT:-/opt/katcha}"
ENV_FILE="${KATCHA_ENV_FILE:-/etc/katcha/katcha.env}"
RESTORE_ENV_FILE="${KATCHA_RESTORE_ENV_FILE:-/etc/katcha/restore.env}"
COMPOSE_FILE="${KATCHA_PRODUCTION_COMPOSE_FILE:-$REPO_ROOT/deploy/docker-compose.production.yml}"
BACKUP_ROOT="${KATCHA_BACKUP_LOCAL_ROOT:-/srv/katcha/backups-local}"
RESTORE_ROOT="$BACKUP_ROOT/restore-test"

exec 9>/run/katcha-postgres-restore-test.lock
if ! flock -n 9; then
  echo "Katcha restore test already running; exiting."
  exit 0
fi

compose=(
  docker compose
  --project-name katcha-production
  --env-file "$ENV_FILE"
  -f "$COMPOSE_FILE"
)

rm -rf "$RESTORE_ROOT"
mkdir -p "$RESTORE_ROOT"
chmod 700 "$RESTORE_ROOT"

"${compose[@]}" --profile ops run --rm --no-deps backup-reader \
  python -m katcha.ops.disaster_backup download-latest \
  --destination /backup/restore-test

backup_dir="$(find "$RESTORE_ROOT" -mindepth 1 -maxdepth 1 -type d -print | sort | tail -n 1)"
if [[ -z "$backup_dir" || ! -f "$backup_dir/manifest.json" ]]; then
  echo "Restore test could not locate the downloaded backup." >&2
  exit 30
fi

plan="$RESTORE_ROOT/restore-plan.tsv"
"${compose[@]}" --profile ops run --rm --no-deps backup-reader \
  python -m katcha.ops.disaster_backup restore-plan \
  --directory "/backup/restore-test/$(basename "$backup_dir")" > "$plan"

test -s "$plan"

container="katcha-restore-test-$(cat /proc/sys/kernel/random/uuid | cut -c1-12)"
password="$(cat /proc/sys/kernel/random/uuid)$(cat /proc/sys/kernel/random/uuid)"
cleanup() {
  docker rm -f "$container" >/dev/null 2>&1 || true
  rm -rf "$RESTORE_ROOT"
}
trap cleanup EXIT

docker run -d \
  --name "$container" \
  -e POSTGRES_PASSWORD="$password" \
  postgres:16-alpine >/dev/null

for attempt in $(seq 1 60); do
  if docker exec "$container" pg_isready -U postgres >/dev/null 2>&1; then
    break
  fi
  if [[ "$attempt" -eq 60 ]]; then
    docker logs "$container" >&2
    exit 31
  fi
  sleep 1
done

restored=0
alembic_seen=0
while IFS=$'\t' read -r filename database; do
  [[ -n "$filename" && -n "$database" ]] || continue
  docker exec "$container" createdb -U postgres "$database"
  docker cp "$backup_dir/$filename" "$container:/tmp/$filename" >/dev/null
  docker exec "$container" pg_restore \
    -U postgres \
    --exit-on-error \
    --no-owner \
    --no-acl \
    -d "$database" \
    "/tmp/$filename"

  table_count="$(
    docker exec "$container" psql -U postgres -d "$database" -At -v ON_ERROR_STOP=1 \
      -c "SELECT count(*) FROM pg_catalog.pg_tables WHERE schemaname NOT IN ('pg_catalog','information_schema');"
  )"
  if [[ ! "$table_count" =~ ^[0-9]+$ || "$table_count" -lt 1 ]]; then
    echo "Restored database $database contains no application tables." >&2
    exit 32
  fi

  has_alembic="$(
    docker exec "$container" psql -U postgres -d "$database" -At -v ON_ERROR_STOP=1 \
      -c "SELECT CASE WHEN to_regclass('public.alembic_version') IS NULL THEN 0 ELSE 1 END;"
  )"
  if [[ "$has_alembic" == "1" ]]; then
    alembic_seen=1
    docker exec "$container" psql -U postgres -d "$database" -At -v ON_ERROR_STOP=1 \
      -c "SELECT version_num FROM alembic_version;" >/dev/null
  fi
  restored=$((restored + 1))
done < "$plan"

if [[ "$restored" -lt 1 ]]; then
  echo "Restore test did not restore any databases." >&2
  exit 33
fi
if [[ "$alembic_seen" -ne 1 ]]; then
  echo "Restore test did not find the Katcha Alembic schema." >&2
  exit 34
fi

echo "Katcha disaster restore test passed for $restored databases."
