#!/usr/bin/env bash
set -euo pipefail

STATUS="${1:-}"
TERMINAL="${2:-false}"
COORDINATOR_URL="${KATCHA_RECOVERY_COORDINATOR_URL:-}"
TOKEN="${KATCHA_RECOVERY_CANDIDATE_TOKEN:-}"
RUN_ID="${GITHUB_RUN_ID:-local}"

if [[ -z "$STATUS" ]]; then
    echo "Bootstrap heartbeat status is required." >&2
    exit 2
fi

if [[ -z "$COORDINATOR_URL" || -z "$TOKEN" ]]; then
    echo "Bootstrap heartbeat skipped: coordinator URL/token is not configured."
    exit 0
fi

payload="$(
    jq -cn \
        --arg run_id "$RUN_ID" \
        --arg status "$STATUS" \
        --argjson terminal "$TERMINAL" \
        '{run_id:$run_id,status:$status,terminal:$terminal}'
)"

if ! curl \
    --fail-with-body \
    --silent \
    --show-error \
    --retry 2 \
    --retry-delay 2 \
    --retry-all-errors \
    --max-time 15 \
    -H "Authorization: Bearer $TOKEN" \
    -H 'Content-Type: application/json' \
    -d "$payload" \
    "${COORDINATOR_URL%/}/v1/bootstrap/heartbeat" >/dev/null
then
    echo "WARNING: bootstrap heartbeat could not reach the Cloudflare coordinator." >&2
    exit 0
fi

echo "Bootstrap heartbeat recorded: $STATUS"
