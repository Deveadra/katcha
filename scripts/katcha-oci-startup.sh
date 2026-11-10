#!/usr/bin/env bash
# Small, tracked WSL entry point for the existing OCI Bastion operator helper.
# Does not bootstrap production, log in implicitly, or create compute/disks.
set -euo pipefail
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
command="${1:-connect}"
if (( $# > 0 )); then
  shift
fi
case "$command" in
  connect)
    printf '== Existing OCI Bastion connection ==\n'
    exec bash "$script_dir/katcha-oci-admin.sh" connect "$@"
    ;;
  status|doctor|audit|metrics|disconnect|reauth|repair-access)
    exec bash "$script_dir/katcha-oci-admin.sh" "$command" "$@"
    ;;
  help|-h|--help)
    exec bash "$script_dir/katcha-oci-admin.sh" --help
    ;;
  *)
    printf 'Unrecognized command: %s\n' "$command" >&2
    exec bash "$script_dir/katcha-oci-admin.sh" --help
    ;;
esac
