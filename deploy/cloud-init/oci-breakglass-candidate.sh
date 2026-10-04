#!/usr/bin/env bash
set -euo pipefail

log() {
  printf '[katcha-breakglass-bootstrap] %s\n' "$1"
}

install_runtime() {
  if command -v apt-get >/dev/null 2>&1; then
    export DEBIAN_FRONTEND=noninteractive
    apt-get update -qq
    apt-get install -y -qq \
      ca-certificates curl e2fsprogs openssh-server \
      python3 python3-venv docker.io docker-compose-v2
  elif command -v dnf >/dev/null 2>&1; then
    dnf install -y \
      ca-certificates curl e2fsprogs openssh-server \
      python3 docker docker-compose-plugin
  else
    log "Unsupported recovery image: apt-get/dnf missing"
    exit 30
  fi
}

install_runtime
systemctl enable --now docker
systemctl enable --now ssh 2>/dev/null || systemctl enable --now sshd
ssh-keygen -A

host_key=/etc/ssh/ssh_host_ed25519_key.pub
if [[ ! -s "$host_key" ]]; then
  log "SSH ed25519 host key was not generated"
  exit 31
fi

fingerprint="$(ssh-keygen -lf "$host_key" -E sha256 | awk '{print $2}')"
if [[ ! "$fingerprint" =~ ^SHA256: ]]; then
  log "Could not determine SSH host-key fingerprint"
  exit 32
fi

install -d -m 0700 /var/lib/katcha-breakglass
touch /var/lib/katcha-breakglass/cloud-init-ready
chmod 0600 /var/lib/katcha-breakglass/cloud-init-ready

# The fingerprint is not secret. It is written to the OCI serial console so the
# off-OCI controller can pin the first SSH connection without TOFU.
printf 'KATCHA_BREAKGLASS_SSH_HOST_FINGERPRINT=%s\n' "$fingerprint" \
  > /dev/console

log "Minimal no-secret bootstrap is ready for authenticated delivery"
