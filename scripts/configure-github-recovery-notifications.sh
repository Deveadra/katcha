#!/usr/bin/env bash
set -euo pipefail

REPO="${KATCHA_GITHUB_REPO:-Deveadra/katcha}"

require_command() {
    if ! command -v "$1" >/dev/null 2>&1; then
        echo "Missing required command: $1" >&2
        exit 3
    fi
}

require_command gh

if ! gh auth status --hostname github.com >/dev/null 2>&1; then
    echo "GitHub CLI is not authenticated." >&2
    exit 4
fi

AUTOMATION_TOKEN="${KATCHA_GITHUB_AUTOMATION_TOKEN_INPUT:-}"
BOT_TOKEN="${KATCHA_TELEGRAM_BOT_TOKEN_INPUT:-}"
CHAT_ID="${KATCHA_TELEGRAM_CHAT_ID_INPUT:-}"
THREAD_ID="${KATCHA_TELEGRAM_THREAD_ID_INPUT:-}"

if [[ -z "$AUTOMATION_TOKEN" ]]; then
    read -r -s -p "Fine-grained GitHub automation PAT: " AUTOMATION_TOKEN
    echo
fi
if [[ -z "$BOT_TOKEN" ]]; then
    read -r -s -p "Telegram bot token: " BOT_TOKEN
    echo
fi
if [[ -z "$CHAT_ID" ]]; then
    read -r -p "Telegram chat ID: " CHAT_ID
fi
if [[ -z "$THREAD_ID" ]]; then
    read -r -p "Telegram topic/thread ID (optional; press Enter for none): " THREAD_ID
fi

if [[ ${#AUTOMATION_TOKEN} -lt 20 ]]; then
    echo "GitHub automation PAT looks invalid or too short." >&2
    exit 10
fi
if [[ "$BOT_TOKEN" != *:* || ${#BOT_TOKEN} -lt 20 ]]; then
    echo "Telegram bot token does not look valid." >&2
    exit 11
fi
if [[ -z "$CHAT_ID" ]]; then
    echo "Telegram chat ID is required." >&2
    exit 12
fi

echo "Writing GitHub/Telegram notification secrets without printing their values..."
printf '%s' "$AUTOMATION_TOKEN" |
    gh secret set KATCHA_GITHUB_AUTOMATION_TOKEN --repo "$REPO"
printf '%s' "$BOT_TOKEN" |
    gh secret set KATCHA_TELEGRAM_BOT_TOKEN --repo "$REPO"
printf '%s' "$CHAT_ID" |
    gh secret set KATCHA_TELEGRAM_CHAT_ID --repo "$REPO"

if [[ -n "$THREAD_ID" ]]; then
    gh variable set KATCHA_TELEGRAM_THREAD_ID --repo "$REPO" --body "$THREAD_ID"
else
    gh variable delete KATCHA_TELEGRAM_THREAD_ID --repo "$REPO" >/dev/null 2>&1 || true
fi

echo "Validating that the automation PAT can update repository variables..."
GH_TOKEN="$AUTOMATION_TOKEN"     gh variable set KATCHA_GITHUB_AUTOMATION_TOKEN_VALIDATED         --repo "$REPO"         --body "true"

previous_run_id="$(
    gh run list         --repo "$REPO"         --workflow telegram-notification-test.yml         --event workflow_dispatch         --limit 1         --json databaseId         --jq '.[0].databaseId // empty'
)"

echo "Dispatching the Telegram acceptance test using the automation PAT..."
GH_TOKEN="$AUTOMATION_TOKEN"     gh workflow run telegram-notification-test.yml         --repo "$REPO"         --ref main

run_id=""
for _ in {1..30}; do
    candidate="$(
        gh run list             --repo "$REPO"             --workflow telegram-notification-test.yml             --event workflow_dispatch             --limit 1             --json databaseId             --jq '.[0].databaseId // empty'
    )"
    if [[ -n "$candidate" && "$candidate" != "$previous_run_id" ]]; then
        run_id="$candidate"
        break
    fi
    sleep 2
done

if [[ -z "$run_id" ]]; then
    echo "Could not identify the Telegram acceptance workflow run." >&2
    exit 13
fi

echo "Telegram acceptance run: $run_id"
if ! gh run watch "$run_id" --repo "$REPO" --exit-status; then
    echo "Telegram acceptance failed. Bootstrap polling should remain disabled until this is fixed." >&2
    exit 14
fi

echo
echo "GitHub automation PAT permissions and Telegram delivery are verified."
echo "Required repository secrets are configured."
