#!/usr/bin/env bash
# Install independently of application cutover, from accepted main only.
set -euo pipefail
mode="${1:-}"
if [[ $# -ne 1 || ( "$mode" != --install && "$mode" != --enable ) || "$EUID" -ne 0 ]]; then
  echo 'Usage: sudo bash install-operations-monitor.sh --install|--enable' >&2
  exit 2
fi
if [[ "$(hostname)" != katcha-prod-primary-001 || ! -d /opt/katcha/.git ]]; then
  echo 'STOP: requires the verified primary checkout.' >&2
  exit 10
fi
root=/opt/katcha
git -C "$root" fetch origin main
target="$(git -C "$root" rev-parse origin/main)"
[[ "$target" =~ ^[0-9a-f]{40}$ ]]
staging="$(mktemp -d)"
trap 'rm -rf "$staging"' EXIT
for path in src/katcha/ops/operations_health.py src/katcha/ops/production_release_observer.py src/katcha/ops/release_eligibility.py deploy/systemd/katcha-operations-monitor.service deploy/systemd/katcha-operations-monitor.timer; do
  mkdir -p "$staging/$(dirname "$path")"
  git -C "$root" show "$target:$path" > "$staging/$path"
  test -s "$staging/$path"
done
python3 -m py_compile "$staging"/src/katcha/ops/*.py
for tool in /usr/local/sbin/katcha-production-deploy /usr/local/sbin/katcha-production-deploy-notify; do
  if [[ ! -f "$tool" || -L "$tool" || "$(stat -c %u "$tool")" != 0 ]]; then
    echo 'STOP: install the existing reviewed deployment tools first.' >&2
    exit 11
  fi
done
for path in /srv/katcha/operations /srv/katcha/operations/public /usr/local/lib/katcha-operations /usr/local/lib/katcha-operations/katcha /usr/local/lib/katcha-operations/katcha/ops; do
  if [[ -L "$path" ]]; then
    echo 'STOP: unexpected symlink in monitor installation paths.' >&2
    exit 12
  fi
done
install -d -o root -g root -m 0755 /srv/katcha/operations /srv/katcha/operations/public
install -d -o root -g root -m 0755 /usr/local/lib/katcha-operations/katcha/ops
touch "$staging/__init__.py"
install -o root -g root -m 0644 "$staging/__init__.py" /usr/local/lib/katcha-operations/katcha/__init__.py
install -o root -g root -m 0644 "$staging/__init__.py" /usr/local/lib/katcha-operations/katcha/ops/__init__.py
was_active=false
if systemctl is-active --quiet katcha-operations-monitor.timer; then
  was_active=true
fi
if ! systemctl stop katcha-operations-monitor.timer katcha-operations-monitor.service 2>/dev/null; then
  if systemctl is-active --quiet katcha-operations-monitor.timer || systemctl is-active --quiet katcha-operations-monitor.service; then
    echo 'STOP: could not stop the existing monitor for installation.' >&2
    exit 13
  fi
fi
install -o root -g root -m 0644 "$staging"/src/katcha/ops/*.py /usr/local/lib/katcha-operations/katcha/ops/
install -o root -g root -m 0644 "$staging"/deploy/systemd/* /etc/systemd/system/
printf '%s\n' "$target" > "$staging/source-sha"
install -o root -g root -m 0644 "$staging/source-sha" /usr/local/lib/katcha-operations/source-sha
systemctl daemon-reload
if [[ "$mode" == --enable ]]; then
  systemctl enable --now katcha-production-deploy-notify.timer
  systemctl enable --now katcha-operations-monitor.timer
  systemctl start katcha-operations-monitor.service
  systemctl is-active katcha-operations-monitor.timer
elif [[ "$was_active" == true ]]; then
  systemctl start katcha-operations-monitor.timer
fi
echo "OPERATIONS_INSTALLED source_sha=$target mode=$mode"
