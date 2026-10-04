#!/usr/bin/env bash
set -euo pipefail

ROOT="${KATCHA_REPO_ROOT:-/opt/katcha}"
start=false
if [[ "${1:-}" == "--start" ]]; then
  start=true
elif [[ $# -gt 0 ]]; then
  echo "Usage: $0 [--start]" >&2
  exit 2
fi

for unit in   katcha.service   katcha-backup.service   katcha-backup.timer   katcha-restore-test.service   katcha-restore-test.timer; do
  source_path="$ROOT/deploy/systemd/$unit"
  if [[ ! -f "$source_path" ]]; then
    echo "Missing production unit: $source_path" >&2
    exit 20
  fi
  install -m 0644 "$source_path" "/etc/systemd/system/$unit"
done

systemctl daemon-reload
systemctl enable katcha.service
systemctl enable katcha-backup.timer
systemctl enable katcha-restore-test.timer

if [[ "$start" == true ]]; then
  systemctl start katcha.service
  systemctl start katcha-backup.timer
  systemctl start katcha-restore-test.timer
fi

systemctl is-enabled katcha.service katcha-backup.timer katcha-restore-test.timer
