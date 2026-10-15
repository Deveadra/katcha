#!/usr/bin/env bash
# One-time, explicitly operator-initiated provisioning; never restarts Katcha.
set -euo pipefail

if [[ "${1:-}" != "--install" || $# -ne 1 || "$EUID" -ne 0 ]]; then
  echo "Usage: sudo bash install-production-deployer.sh --install" >&2
  exit 2
fi

root=/opt/katcha
if [[ ! -d "$root/.git" ]] || [[ ! -f /etc/katcha/katcha.env ]]; then
  echo "STOP: verified OCI production checkout/environment not present." >&2
  exit 10
fi
if [[ "$(hostname)" != "katcha-prod-primary-001" ]]; then
  echo "STOP: installer may run only on the existing primary." >&2
  exit 11
fi
if ! id katcha-deploy >/dev/null 2>&1; then
  echo "STOP: create the dedicated unprivileged runner user first." >&2
  exit 12
fi
if id -nG katcha-deploy | grep -Eq '(^| )(sudo|docker|root)( |$)'; then
  echo "STOP: deployment runner must not have general sudo or Docker access." >&2
  exit 13
fi

cd "$root"
git fetch origin main
target="$(git rev-parse origin/main)"
if [[ ! "$target" =~ ^[0-9a-f]{40}$ ]]; then
  echo "STOP: invalid accepted source revision." >&2
  exit 14
fi
if [[ -n "$(git status --porcelain)" ]]; then
  echo "STOP: production working tree is dirty." >&2
  exit 15
fi

# Install only a fixed-path root-owned executor from the reviewed main tree.
# Nothing is piped into bash and the active service checkout is never changed.
staging="$(mktemp --suffix=.py)"
trap 'rm -f "$staging"' EXIT
git show "$target:deploy/scripts/production-deploy.py" > "$staging"
python3 -m py_compile "$staging"
install -o root -g root -m 0755 "$staging" /usr/local/sbin/katcha-production-deploy

sudoers="$(mktemp)"
trap 'rm -f "$staging" "$sudoers"' EXIT
printf '%s\n' 'katcha-deploy ALL=(root) NOPASSWD: /usr/local/sbin/katcha-production-deploy *' > "$sudoers"
chmod 0440 "$sudoers"
visudo -cf "$sudoers" >/dev/null
install -o root -g root -m 0440 "$sudoers" /etc/sudoers.d/katcha-production-deploy

# Root-only opt-in is deliberately NOT created by this installer.
echo "DEPLOYER_INSTALLED source_sha=$target production_gate=UNCHANGED runner=UNREGISTERED_BY_SCRIPT"
echo "Requires an approved production environment, restricted self-hosted runner,"
echo "and a separate root-only enabled gate."
