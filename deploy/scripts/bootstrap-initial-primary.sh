#!/usr/bin/env bash
set -euo pipefail

DEVICE="${KATCHA_OCI_DATA_VOLUME_DEVICE_PATH:-/dev/oracleoci/oraclevdb}"
RELEASE_SHA="${KATCHA_RELEASE_SHA:-}"
REPO_URL="${KATCHA_REPOSITORY_URL:-https://github.com/Deveadra/katcha.git}"
REPO_ROOT="${KATCHA_REPO_ROOT:-/opt/katcha}"
STATE_FILE="/srv/katcha/initial-bootstrap.json"

log() {
    printf '[katcha-initial-bootstrap] %s\n' "$1"
}

fail() {
    printf '[katcha-initial-bootstrap] ERROR: %s\n' "$1" >&2
    exit 1
}

if [[ "$(id -u)" -ne 0 ]]; then
    fail "run this script as root (sudo)"
fi

if [[ ! "$RELEASE_SHA" =~ ^[0-9a-fA-F]{40}$ ]]; then
    fail "KATCHA_RELEASE_SHA must be an exact 40-character Git commit SHA"
fi

if [[ ! -b "$DEVICE" ]]; then
    fail "durable block device is missing: $DEVICE"
fi

install_runtime() {
    if command -v apt-get >/dev/null 2>&1; then
        export DEBIAN_FRONTEND=noninteractive
        apt-get update -qq
        apt-get install -y -qq \
            ca-certificates curl e2fsprogs git jq python3 python3-pip python3-venv \
            docker.io docker-compose-v2
    elif command -v dnf >/dev/null 2>&1; then
        dnf install -y \
            ca-certificates curl e2fsprogs git jq python3 python3-pip \
            docker docker-compose-plugin
    else
        fail "unsupported image: apt-get/dnf is unavailable"
    fi

    systemctl enable --now docker
    docker compose version >/dev/null 2>&1 ||
        fail "Docker Compose v2 is required"
}

filesystem_type="$(blkid -s TYPE -o value "$DEVICE" 2>/dev/null || true)"
filesystem_uuid="$(blkid -s UUID -o value "$DEVICE" 2>/dev/null || true)"

if [[ -z "$filesystem_type" ]]; then
    log "Formatting the newly acquired durable volume as ext4"
    mkfs.ext4 -F "$DEVICE" >/dev/null
    filesystem_type="$(blkid -s TYPE -o value "$DEVICE")"
    filesystem_uuid="$(blkid -s UUID -o value "$DEVICE")"
else
    log "Durable volume already has filesystem type $filesystem_type; preserving it"
fi

if [[ -z "$filesystem_uuid" ]]; then
    fail "durable volume has no filesystem UUID after preparation"
fi

mkdir -p /srv/katcha
if mountpoint -q /srv/katcha; then
    mounted_source="$(findmnt -n -o SOURCE /srv/katcha)"
    mounted_uuid="$(blkid -s UUID -o value "$mounted_source" 2>/dev/null || true)"
    if [[ "$mounted_uuid" != "$filesystem_uuid" ]]; then
        fail "/srv/katcha is already mounted from a different filesystem"
    fi
else
    mount "$DEVICE" /srv/katcha
fi

if ! grep -Fq "UUID=$filesystem_uuid /srv/katcha " /etc/fstab; then
    printf 'UUID=%s /srv/katcha %s defaults,_netdev,nofail 0 2\n' \
        "$filesystem_uuid" "$filesystem_type" >> /etc/fstab
fi

for directory in postgres handoff recovery backups-local; do
    mkdir -p "/srv/katcha/$directory"
done

install_runtime

if [[ ! -d "$REPO_ROOT/.git" ]]; then
    log "Cloning Katcha repository"
    git clone "$REPO_URL" "$REPO_ROOT"
fi

git -C "$REPO_ROOT" remote get-url origin >/dev/null 2>&1 ||
    fail "$REPO_ROOT is not a valid Git checkout"
git -C "$REPO_ROOT" fetch --force origin "$RELEASE_SHA"
git -C "$REPO_ROOT" checkout --detach "$RELEASE_SHA"

mkdir -p /etc/katcha/aws
chmod 0700 /etc/katcha /etc/katcha/aws
umask 077

cat > "$STATE_FILE.tmp" <<EOF
{
  "stage": "foundation-ready",
  "release_sha": "$RELEASE_SHA",
  "data_volume_device": "$DEVICE",
  "data_volume_fs_uuid": "$filesystem_uuid",
  "repository_root": "$REPO_ROOT"
}
EOF
mv "$STATE_FILE.tmp" "$STATE_FILE"
chmod 0600 "$STATE_FILE"

log "Initial OCI host foundation is ready"
printf 'KATCHA_INITIAL_FS_UUID=%s\n' "$filesystem_uuid"
printf 'KATCHA_INITIAL_RELEASE_SHA=%s\n' "$RELEASE_SHA"
printf 'KATCHA_INITIAL_STAGE=foundation-ready\n'
