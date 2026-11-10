#!/usr/bin/env bash
# WSL operator entry point. No-arg invocation opens the existing primary SSH.
# A completed GitHub CLI device login is independent from OCI browser login.
set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
ADMIN="$SCRIPT_DIR/katcha-oci-admin.sh"
[[ -f "$ADMIN" ]] || { printf 'ERROR: Missing %s\n' "$ADMIN" >&2; exit 2; }

usage() {
  cat <<'HELP'
Usage: bash scripts/katcha-oci-startup.sh [ssh|connect|status|doctor|audit|metrics|disconnect|reauth|repair-access|github-doctor|github-login|diagnostics]

With no arguments, verify/reuse the managed Bastion tunnel then open interactive
SSH to the verified primary. 'connect' only establishes the tunnel.
'status', 'doctor', 'github-doctor', 'audit' and 'diagnostics' do not mutate OCI.
'github-login' explicitly starts GitHub's device login without launching WSL Chromium.
'diagnostics' shows the sanitized local operator event log.
'repair-access' needs explicit interactive UPDATE approval.
HELP
}

command="${1:-ssh}"
if (( $# > 0 )); then
  shift
fi

case "$command" in
  -h|--help|help)
    usage
    ;;
  github-login)
    command -v gh >/dev/null || { echo 'ERROR: GitHub CLI (gh) is unavailable.' >&2; exit 2; }
    echo 'Open https://github.com/login/device in your normal Windows browser.'
    echo 'Enter the one-time code printed by gh. Do not interrupt the terminal'
    echo 'after browser authorization; gh must finish storing the new credential.'
    echo 'This command ignores inherited GH_TOKEN/GITHUB_TOKEN only for this login.'
    # No GUI launcher, browser subprocess, token printing, or implicit logout.
    env -u GH_TOKEN -u GITHUB_TOKEN GH_BROWSER=/bin/true BROWSER=/bin/true \
      gh auth login --hostname github.com --git-protocol ssh --skip-ssh-key --web
    echo 'GitHub login completed; verifying repository variable access.'
    env -u GH_TOKEN -u GITHUB_TOKEN bash "$ADMIN" github-doctor
    ;;
  connect|status|doctor|audit|metrics|disconnect|reauth|repair-access|github-doctor|diagnostics)
    exec bash "$ADMIN" "$command" "$@"
    ;;
  ssh)
    if (( $# != 0 )); then
      echo 'ERROR: Unexpected arguments for SSH. Use the interactive login directly.' >&2
      exit 2
    fi
    command -v ssh >/dev/null || { echo 'ERROR: ssh is unavailable.' >&2; exit 2; }
    printf '\n== Existing OCI Bastion tunnel ==\n'
    if ! bash "$ADMIN" connect; then
      echo 'ERROR: Bastion connection failed; no SSH session started.' >&2
      echo 'Run: bash scripts/katcha-oci-startup.sh github-doctor' >&2
      echo 'Then: bash scripts/katcha-oci-startup.sh doctor' >&2
      echo 'Only if doctor confirms a CIDR mismatch, explicitly run repair-access.' >&2
      exit 2
    fi
    printf '\n== SSH to verified OCI production primary ==\n'
    exec ssh -F /dev/null -4 \
      -i "$HOME/.ssh/katcha-oci" \
      -o BatchMode=no \
      -o IdentitiesOnly=yes \
      -o StrictHostKeyChecking=yes \
      -o "UserKnownHostsFile=$HOME/.config/katcha/production/bastion/target_known_hosts" \
      -p 22022 ubuntu@127.0.0.1
    ;;
  *)
    printf 'ERROR: Unknown command: %s\n' "$command" >&2
    usage >&2
    exit 2
    ;;
esac
