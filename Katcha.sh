#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
if ! command -v python3 >/dev/null; then
    echo 'Katcha requires Python 3.11+ and Docker Desktop with WSL integration.' >&2
    exit 1
fi
exec python3 launcher/runtime.py "$@"
