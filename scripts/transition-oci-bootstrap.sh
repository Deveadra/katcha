#!/usr/bin/env bash
set -euo pipefail

REPO="${GITHUB_REPOSITORY:-${KATCHA_GITHUB_REPO:-Deveadra/katcha}}"

require() {
    local name="$1"
    local value="${!name:-}"
    if [[ -z "$value" ]]; then
        echo "Missing required bootstrap transition value: $name" >&2
        exit 20
    fi
}

require GH_TOKEN
require KATCHA_BOOTSTRAP_INSTANCE_ID
require KATCHA_BOOTSTRAP_AVAILABILITY_DOMAIN
require KATCHA_BOOTSTRAP_SUBNET_ID
require KATCHA_BOOTSTRAP_VOLUME_ID
require KATCHA_BOOTSTRAP_ATTACHMENT_ID
require KATCHA_OCI_AVAILABILITY_DOMAIN
require KATCHA_OCI_SUBNET_ID
require KATCHA_OCI_CROSS_AD_TARGETS_JSON

all_targets="$(
    jq -cn         --arg primary_ad "$KATCHA_OCI_AVAILABILITY_DOMAIN"         --arg primary_subnet "$KATCHA_OCI_SUBNET_ID"         --argjson alternates "$KATCHA_OCI_CROSS_AD_TARGETS_JSON"         '[{availability_domain:$primary_ad,subnet_id:$primary_subnet}] + $alternates'
)"

new_alternates="$(
    printf '%s' "$all_targets" |
        jq -c             --arg active_ad "$KATCHA_BOOTSTRAP_AVAILABILITY_DOMAIN"             '[.[] | select(.availability_domain != $active_ad)]'
)"

target_count="$(printf '%s' "$all_targets" | jq 'length')"
alternate_count="$(printf '%s' "$new_alternates" | jq 'length')"
if [[ "$target_count" -ne 3 || "$alternate_count" -ne 2 ]]; then
    echo "Bootstrap transition refused unexpected AD target topology." >&2
    exit 21
fi

echo "Recording acquired OCI host as the production-primary target..."
gh variable set KATCHA_OCI_AVAILABILITY_DOMAIN     --repo "$REPO"     --body "$KATCHA_BOOTSTRAP_AVAILABILITY_DOMAIN"
gh variable set KATCHA_OCI_SUBNET_ID     --repo "$REPO"     --body "$KATCHA_BOOTSTRAP_SUBNET_ID"
gh variable set KATCHA_OCI_DATA_VOLUME_ID     --repo "$REPO"     --body "$KATCHA_BOOTSTRAP_VOLUME_ID"
gh variable set KATCHA_OCI_PRIMARY_INSTANCE_ID     --repo "$REPO"     --body "$KATCHA_BOOTSTRAP_INSTANCE_ID"
gh variable set KATCHA_OCI_CROSS_AD_TARGETS_JSON     --repo "$REPO"     --body "$new_alternates"
gh variable set KATCHA_OCI_BOOTSTRAP_ACQUIRED     --repo "$REPO"     --body "true"
gh variable set KATCHA_OCI_BOOTSTRAP_ACQUIRED_AT     --repo "$REPO"     --body "$(date -u +'%Y-%m-%dT%H:%M:%SZ')"

notification_sent="$(
    gh variable get KATCHA_OCI_BOOTSTRAP_NOTIFICATION_SENT         --repo "$REPO" 2>/dev/null || true
)"

if [[ "$notification_sent" != "true" ]]; then
    message="$(
        cat <<EOF
Katcha secured OCI A1 capacity.

Host: VM.Standard.A1.Flex — 2 OCPU / 12 GB
Availability Domain: $KATCHA_BOOTSTRAP_AVAILABILITY_DOMAIN
Durable storage: attached
Public IP: none

Capacity polling is stopping and host provisioning can continue.
EOF
    )"

    KATCHA_TELEGRAM_BOT_TOKEN="${KATCHA_TELEGRAM_BOT_TOKEN:-}"     KATCHA_TELEGRAM_CHAT_ID="${KATCHA_TELEGRAM_CHAT_ID:-}"     KATCHA_TELEGRAM_THREAD_ID="${KATCHA_TELEGRAM_THREAD_ID:-}"         bash scripts/notify-telegram.sh "$message"

    if [[ -n "${KATCHA_TELEGRAM_BOT_TOKEN:-}" && -n "${KATCHA_TELEGRAM_CHAT_ID:-}" ]]; then
        gh variable set KATCHA_OCI_BOOTSTRAP_NOTIFICATION_SENT             --repo "$REPO"             --body "true"
    fi
fi

# This is intentionally last. If metadata persistence or a configured Telegram
# notification fails, the next scheduled run reuses the acquired instance and
# retries this transition instead of silently abandoning the handoff.
gh variable set KATCHA_OCI_BOOTSTRAP_POLL_ENABLED     --repo "$REPO"     --body "false"

echo "Bootstrap capacity transition complete; scheduled capacity polling disabled."
