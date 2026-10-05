#!/usr/bin/env bash
set -euo pipefail

COORDINATOR_URL="${KATCHA_RECOVERY_COORDINATOR_URL:-https://recovery.katcha.stream}"
ADMIN_TOKEN="${KATCHA_RECOVERY_ADMIN_TOKEN:-}"
ENABLED=true

if [[ "${1:-}" == "--disable" ]]; then
    ENABLED=false
elif [[ -n "${1:-}" ]]; then
    echo "Usage: $0 [--disable]" >&2
    exit 2
fi

if [[ -z "$ADMIN_TOKEN" ]]; then
    read -r -s -p "Katcha recovery admin token: " ADMIN_TOKEN
    echo
fi
if [[ ${#ADMIN_TOKEN} -lt 20 ]]; then
    echo "Recovery admin token looks invalid or too short." >&2
    exit 3
fi

payload="$(
    jq -cn \
        --argjson enabled "$ENABLED" \
        '{
          enabled:$enabled,
          check_interval_seconds:60,
          stale_after_seconds:900,
          redispatch_cooldown_seconds:1200
        }'
)"

echo "Configuring independent Cloudflare bootstrap watchdog..."
curl \
    --fail-with-body \
    --silent \
    --show-error \
    --max-time 20 \
    -H "Authorization: Bearer $ADMIN_TOKEN" \
    -H 'Content-Type: application/json' \
    -d "$payload" \
    "${COORDINATOR_URL%/}/v1/bootstrap/watchdog/configure" |
    jq '{bootstrap_watchdog: .bootstrap_watchdog}'

echo
echo "Bootstrap watchdog status:"
curl \
    --fail-with-body \
    --silent \
    --show-error \
    --max-time 20 \
    -H "Authorization: Bearer $ADMIN_TOKEN" \
    "${COORDINATOR_URL%/}/v1/bootstrap/watchdog/status" |
    jq '{bootstrap_watchdog: .bootstrap_watchdog}'

if [[ "$ENABLED" == "true" ]]; then
    echo
    echo "Cloudflare will rescue a stale bootstrap chain after 15 minutes,"
    echo "with at most one rescue dispatch per 20 minutes until heartbeats resume."
else
    echo
    echo "Cloudflare bootstrap watchdog is disabled."
fi
