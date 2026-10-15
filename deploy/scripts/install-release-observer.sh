#!/usr/bin/env bash
set -euo pipefail

ROOT="${KATCHA_REPO_ROOT:-/opt/katcha}"
MODE="${1:---help}"
if [[ $# -gt 1 || ( "$MODE" != "--install" && "$MODE" != "--enable" ) ]]; then
  echo "Usage: sudo bash deploy/scripts/install-release-observer.sh --install|--enable" >&2
  echo "--install installs the read-only units without enabling the timer" >&2
  echo "--enable also enables the hourly read-only observer timer" >&2
  exit 2
fi

if [[ "$EUID" != 0 || "$ROOT" != /opt/katcha ]]; then
  echo "STOP: requires root on the verified /opt/katcha production checkout" >&2
  exit 10
fi
for path in \
  "$ROOT/src/katcha/ops/production_release_observer.py" \
  "$ROOT/src/katcha/ops/release_eligibility.py" \
  "$ROOT/deploy/systemd/katcha-release-observer.service" \
  "$ROOT/deploy/systemd/katcha-release-observer.timer" \
  /etc/katcha/katcha.env; do
  if [[ ! -f "$path" || -L "$path" ]]; then
    echo "STOP: required regular file missing or symlinked" >&2
    exit 11
  fi
done

# This script never invokes, edits or restarts the production supervisor.
install -m 0644 \
  "$ROOT/deploy/systemd/katcha-release-observer.service" \
  /etc/systemd/system/katcha-release-observer.service
install -m 0644 \
  "$ROOT/deploy/systemd/katcha-release-observer.timer" \
  /etc/systemd/system/katcha-release-observer.timer
systemctl daemon-reload
if [[ "$MODE" == "--enable" ]]; then
  systemctl enable --now katcha-release-observer.timer
fi
systemctl is-enabled katcha-release-observer.timer || true
printf 'RELEASE_OBSERVER_INSTALLED mode=%s production_untouched=true\n' "$MODE"
