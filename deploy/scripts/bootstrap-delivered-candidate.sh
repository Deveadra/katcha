#!/usr/bin/env bash
set -euo pipefail

STAGE="${KATCHA_BREAKGLASS_STAGE:-/var/lib/katcha-breakglass/incoming}"
REPO_ROOT="${KATCHA_REPO_ROOT:-/opt/katcha}"
DEPLOYMENT_ID="${KATCHA_DEPLOYMENT_ID:?KATCHA_DEPLOYMENT_ID is required}"
DEPLOYMENT_EPOCH="${KATCHA_DEPLOYMENT_EPOCH:?KATCHA_DEPLOYMENT_EPOCH is required}"
RELEASE_SHA="${KATCHA_RELEASE_SHA:?KATCHA_RELEASE_SHA is required}"
STORAGE_MODE="${KATCHA_RECOVERY_STORAGE_MODE:?KATCHA_RECOVERY_STORAGE_MODE is required}"
DEVICE_PATH="${KATCHA_OCI_DATA_VOLUME_DEVICE_PATH:-/dev/oracleoci/oraclevdb}"
MAX_BACKUP_AGE_SECONDS="${KATCHA_OCI_CROSS_AD_MAX_BACKUP_AGE_SECONDS:-7200}"
PUBLIC_HEALTH_URL="${KATCHA_PUBLIC_HEALTH_URL:?KATCHA_PUBLIC_HEALTH_URL is required}"

log() {
  printf '[katcha-breakglass-install] %s\n' "$1"
}

if [[ "$(id -u)" -ne 0 ]]; then
  echo "Break-glass candidate installer must run as root." >&2
  exit 40
fi

case "$STORAGE_MODE" in
  existing-volume|r2-restore)
    ;;
  *)
    echo "Unsupported recovery storage mode: $STORAGE_MODE" >&2
    exit 41
    ;;
esac

if [[ ! "$DEPLOYMENT_EPOCH" =~ ^[1-9][0-9]*$ ]]; then
  echo "KATCHA_DEPLOYMENT_EPOCH must be positive." >&2
  exit 42
fi
if [[ ! "$RELEASE_SHA" =~ ^[0-9a-f]{40}$ ]]; then
  echo "KATCHA_RELEASE_SHA must be an exact lowercase git SHA." >&2
  exit 43
fi
if [[ ! "$MAX_BACKUP_AGE_SECONDS" =~ ^[0-9]+$ || "$MAX_BACKUP_AGE_SECONDS" -lt 900 ]]; then
  echo "Cross-AD backup freshness must be at least 900 seconds." >&2
  exit 44
fi

for name in katcha.env backup.env restore.env aws.tgz source.tgz; do
  path="$STAGE/$name"
  if [[ ! -f "$path" || -L "$path" || ! -s "$path" ]]; then
    echo "Missing or unsafe delivered payload: $path" >&2
    exit 45
  fi
done

install -d -m 0700 /etc/katcha /etc/katcha/aws
install -m 0600 "$STAGE/katcha.env" /etc/katcha/katcha.env
install -m 0600 "$STAGE/backup.env" /etc/katcha/backup.env
install -m 0600 "$STAGE/restore.env" /etc/katcha/restore.env

tar -tzf "$STAGE/aws.tgz" >/dev/null
rm -rf /etc/katcha/aws/*
tar -xzf "$STAGE/aws.tgz" \
  --no-same-owner \
  --no-same-permissions \
  -C /etc/katcha/aws
chmod -R go-rwx /etc/katcha/aws

if [[ -e "$REPO_ROOT" ]] && find "$REPO_ROOT" -mindepth 1 -print -quit | grep -q .; then
  echo "Refusing to overwrite non-empty repository root: $REPO_ROOT" >&2
  exit 46
fi
install -d -m 0755 "$REPO_ROOT"
tar -tzf "$STAGE/source.tgz" >/dev/null
tar -xzf "$STAGE/source.tgz" \
  --no-same-owner \
  --no-same-permissions \
  -C "$REPO_ROOT"

sed -i \
  -e '/^KATCHA_RELEASE_SHA=/d' \
  -e '/^KATCHA_DEPLOYMENT_ID=/d' \
  -e '/^KATCHA_DEPLOYMENT_EPOCH=/d' \
  /etc/katcha/katcha.env
cat >> /etc/katcha/katcha.env <<EOF
KATCHA_RELEASE_SHA=$RELEASE_SHA
KATCHA_DEPLOYMENT_ID=$DEPLOYMENT_ID
KATCHA_DEPLOYMENT_EPOCH=$DEPLOYMENT_EPOCH
EOF

log "Waiting for recovery data volume"
device=""
for _ in $(seq 1 120); do
  if [[ -b "$DEVICE_PATH" ]]; then
    device="$DEVICE_PATH"
    break
  fi
  sleep 5
done
if [[ -z "$device" ]]; then
  echo "Recovery data device did not appear: $DEVICE_PATH" >&2
  exit 47
fi

if [[ "$STORAGE_MODE" == "r2-restore" ]]; then
  if blkid -s TYPE -o value "$device" 2>/dev/null | grep -q .; then
    echo "Fresh break-glass recovery volume unexpectedly has a filesystem." >&2
    exit 48
  fi
  mkfs.ext4 -F "$device"
fi

filesystem_type="$(blkid -s TYPE -o value "$device" 2>/dev/null || true)"
filesystem_uuid="$(blkid -s UUID -o value "$device" 2>/dev/null || true)"
if [[ -z "$filesystem_type" || -z "$filesystem_uuid" ]]; then
  echo "Recovery data device has no usable filesystem." >&2
  exit 49
fi

install -d -m 0755 /srv/katcha
if ! mountpoint -q /srv/katcha; then
  mount "$device" /srv/katcha
fi
for directory in postgres handoff recovery backups-local; do
  mkdir -p "/srv/katcha/$directory"
done
if ! grep -Fq "UUID=$filesystem_uuid /srv/katcha " /etc/fstab; then
  printf 'UUID=%s /srv/katcha %s defaults,_netdev,nofail 0 2\n' \
    "$filesystem_uuid" "$filesystem_type" >> /etc/fstab
fi

if [[ "$STORAGE_MODE" == "r2-restore" ]]; then
  log "Restoring latest verified R2 recovery point"
  KATCHA_REPO_ROOT="$REPO_ROOT" \
  KATCHA_ENV_FILE=/etc/katcha/katcha.env \
  KATCHA_RESTORE_ENV_FILE=/etc/katcha/restore.env \
  KATCHA_CROSS_AD_MAX_BACKUP_AGE_SECONDS="$MAX_BACKUP_AGE_SECONDS" \
    /bin/bash "$REPO_ROOT/deploy/scripts/postgres-disaster-restore.sh"
fi

KATCHA_REPO_ROOT="$REPO_ROOT" \
  /bin/bash "$REPO_ROOT/deploy/scripts/install-production-units.sh" --start

log "Waiting for local Katcha readiness"
for _ in $(seq 1 180); do
  if curl -fsS --max-time 5 \
      http://127.0.0.1:8000/v1/health/ready >/dev/null; then
    break
  fi
  sleep 5
done
curl -fsS --max-time 5 http://127.0.0.1:8000/v1/health/ready >/dev/null

log "Waiting for public Cloudflare route"
for _ in $(seq 1 120); do
  if curl -fsS --max-time 10 "$PUBLIC_HEALTH_URL" >/dev/null; then
    break
  fi
  sleep 5
done
curl -fsS --max-time 10 "$PUBLIC_HEALTH_URL" >/dev/null

cat > /var/lib/katcha-breakglass/candidate-ready.json <<EOF
{"deployment_id":"$DEPLOYMENT_ID","deployment_epoch":$DEPLOYMENT_EPOCH,"runtime_ready":true,"durable_state_ready":true,"public_route_ready":true}
EOF
chmod 0600 /var/lib/katcha-breakglass/candidate-ready.json

# Delivery material is no longer needed after installation.
rm -rf "$STAGE"
log "Delivered candidate bootstrap completed"
