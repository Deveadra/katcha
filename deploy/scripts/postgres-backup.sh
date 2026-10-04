#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="${KATCHA_REPO_ROOT:-/opt/katcha}"
ENV_FILE="${KATCHA_ENV_FILE:-/etc/katcha/katcha.env}"
BACKUP_ENV_FILE="${KATCHA_BACKUP_ENV_FILE:-/etc/katcha/backup.env}"
COMPOSE_FILE="${KATCHA_PRODUCTION_COMPOSE_FILE:-$REPO_ROOT/deploy/docker-compose.production.yml}"
BACKUP_ROOT="${KATCHA_BACKUP_LOCAL_ROOT:-/srv/katcha/backups-local}"

exec 9>/run/katcha-postgres-backup.lock
if ! flock -n 9; then
  echo "Katcha PostgreSQL backup already running; exiting."
  exit 0
fi

compose=(
  docker compose
  --project-name katcha-production
  --env-file "$ENV_FILE"
  -f "$COMPOSE_FILE"
)

backup_id="$(date -u +%Y%m%dT%H%M%SZ)-$(cat /proc/sys/kernel/random/uuid)"
workdir="$BACKUP_ROOT/$backup_id"
mapping="$workdir/databases.tsv"
mkdir -p "$workdir"
chmod 700 "$workdir"

cleanup_failed() {
  status=$?
  if [[ $status -ne 0 ]]; then
    echo "Backup failed; retaining local recovery files at $workdir" >&2
  fi
}
trap cleanup_failed EXIT

mapfile -t databases < <(
  "${compose[@]}" exec -T postgres sh -lc \
    'psql -U "$POSTGRES_USER" -d postgres -At -v ON_ERROR_STOP=1 -c "SELECT datname FROM pg_database WHERE datistemplate = false AND datallowconn = true AND datname <> '\''postgres'\'' ORDER BY datname"'
)

if [[ ${#databases[@]} -eq 0 ]]; then
  echo "No non-template PostgreSQL databases found; refusing empty backup." >&2
  exit 20
fi

: > "$mapping"
for database in "${databases[@]}"; do
  if [[ "$database" == *$'\t'* || "$database" == *$'\n'* || "$database" == *$'\r'* ]]; then
    echo "Database name contains unsupported control characters." >&2
    exit 21
  fi
  digest="$(printf '%s' "$database" | sha256sum | awk '{print substr($1,1,20)}')"
  filename="db-$digest.dump"
  printf '%s\t%s\n' "$filename" "$database" >> "$mapping"
  echo "Dumping PostgreSQL database: $database"
  "${compose[@]}" exec -T postgres sh -c \
    'pg_dump -U "$POSTGRES_USER" --format=custom --compress=6 --no-owner --no-acl "$1"' \
    sh "$database" > "$workdir/$filename"
  test -s "$workdir/$filename"
done

"${compose[@]}" --profile ops run --rm --no-deps backup-writer \
  python -m katcha.ops.disaster_backup manifest \
  --directory "/backup/$backup_id" \
  --mapping "/backup/$backup_id/databases.tsv" \
  --backup-id "$backup_id" \
  --release-sha "${KATCHA_RELEASE_SHA:?KATCHA_RELEASE_SHA is required}" \
  --deployment-id "${KATCHA_DEPLOYMENT_ID:?KATCHA_DEPLOYMENT_ID is required}" \
  --deployment-epoch "${KATCHA_DEPLOYMENT_EPOCH:?KATCHA_DEPLOYMENT_EPOCH is required}"

"${compose[@]}" --profile ops run --rm --no-deps backup-writer \
  python -m katcha.ops.disaster_backup upload \
  --directory "/backup/$backup_id"

rm -rf "$workdir"
trap - EXIT
echo "Katcha PostgreSQL backup completed: $backup_id"
