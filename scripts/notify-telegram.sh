#!/usr/bin/env bash
set -euo pipefail

MESSAGE="${1:-}"
if [[ -z "$MESSAGE" ]]; then
    MESSAGE="$(cat)"
fi

if [[ -z "$MESSAGE" ]]; then
    echo "Telegram notification message is empty." >&2
    exit 2
fi

BOT_TOKEN="${KATCHA_TELEGRAM_BOT_TOKEN:-}"
CHAT_ID="${KATCHA_TELEGRAM_CHAT_ID:-}"
THREAD_ID="${KATCHA_TELEGRAM_THREAD_ID:-}"
REQUIRED="${KATCHA_TELEGRAM_REQUIRED:-false}"

if [[ -z "$BOT_TOKEN" || -z "$CHAT_ID" ]]; then
    if [[ "$REQUIRED" == "true" ]]; then
        echo "Telegram notification is required but bot token/chat ID is not configured." >&2
        exit 4
    fi
    echo "Telegram notification skipped: bot token/chat ID not configured."
    exit 0
fi

args=(
    --fail-with-body
    --silent
    --show-error
    --max-time 20
    --retry 2
    --retry-delay 2
    --request POST
    --data-urlencode "chat_id=$CHAT_ID"
    --data-urlencode "text=$MESSAGE"
)

if [[ -n "$THREAD_ID" ]]; then
    args+=(--data-urlencode "message_thread_id=$THREAD_ID")
fi

response="$(
    curl "${args[@]}"         "https://api.telegram.org/bot${BOT_TOKEN}/sendMessage"
)"

if command -v jq >/dev/null 2>&1; then
    if ! printf '%s' "$response" | jq -e '.ok == true' >/dev/null; then
        echo "Telegram API did not confirm delivery." >&2
        exit 3
    fi
fi

echo "Telegram notification delivered."
