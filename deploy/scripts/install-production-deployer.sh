#!/usr/bin/env bash
# One-time reviewed host-pull deployer installation: no runner, no restart.
set -euo pipefail

if [[ "${1:-}" != "--install" || $# -ne 1 || "$EUID" -ne 0 ]]; then
  echo "Usage: sudo bash install-production-deployer.sh --install" >&2
  exit 2
fi
root=/opt/katcha
if [[ ! -d "$root/.git" || ! -f /etc/katcha/katcha.env ]]; then
  echo "STOP: verified production checkout/environment missing." >&2
  exit 10
fi
if [[ "$(hostname)" != "katcha-prod-primary-001" ]]; then
  echo "STOP: installer must run on the existing OCI primary." >&2
  exit 11
fi

cd "$root"
git fetch origin main
target="$(git rev-parse origin/main)"
if [[ ! "$target" =~ ^[0-9a-f]{40}$ || -n "$(git status --porcelain)" ]]; then
  echo "STOP: accepted main or production checkout is not safe." >&2
  exit 12
fi

staging="$(mktemp -d)"
trap 'rm -rf "$staging"' EXIT
for path in \
  deploy/scripts/production-deploy.py \
  deploy/scripts/production-deploy-poll.py \
  src/katcha/ops/release_eligibility.py \
  deploy/systemd/katcha-production-deploy-poll.service \
  deploy/systemd/katcha-production-deploy-poll.timer; do
  mkdir -p "$staging/$(dirname "$path")"
  git show "$target:$path" > "$staging/$path"
  test -s "$staging/$path"
done
python3 -m py_compile \
  "$staging/deploy/scripts/production-deploy.py" \
  "$staging/deploy/scripts/production-deploy-poll.py" \
  "$staging/src/katcha/ops/release_eligibility.py"

install -d -o root -g root -m 0700 /usr/local/lib/katcha-production-deploy
install -o root -g root -m 0600 \
  "$staging/src/katcha/ops/release_eligibility.py" \
  /usr/local/lib/katcha-production-deploy/release_eligibility.py

install -o root -g root -m 0755 \
  "$staging/deploy/scripts/production-deploy.py" \
  /usr/local/sbin/katcha-production-deploy
install -o root -g root -m 0755 \
  "$staging/deploy/scripts/production-deploy-poll.py" \
  /usr/local/sbin/katcha-production-deploy-poll
for unit in katcha-production-deploy-poll.service katcha-production-deploy-poll.timer; do
  install -o root -g root -m 0644 "$staging/deploy/systemd/$unit" \
    "/etc/systemd/system/$unit"
done
systemctl daemon-reload

# No gate, signing secret, timer enablement, production service restart,
# Git checkout advancement, OCI authority change or new networking.
echo "DEPLOYER_INSTALLED source_sha=$target enablement=OFF runtime_untouched=true"
