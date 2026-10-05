#!/usr/bin/env bash
set -euo pipefail

REPO="${KATCHA_GITHUB_REPO:-Deveadra/katcha}"
PRIVATE_KEY_FILE="${OCI_API_PRIVATE_KEY_FILE:-$HOME/.config/katcha/oci-github-recovery/oci_api_key.pem}"
SSH_PUBLIC_KEY_FILE="${KATCHA_OCI_SSH_PUBLIC_KEY_FILE:-$HOME/.ssh/katcha-oci.pub}"
ENABLE=false

if [[ "${1:-}" == "--enable" ]]; then
    ENABLE=true
elif [[ -n "${1:-}" ]]; then
    echo "Usage: $0 [--enable]" >&2
    exit 2
fi

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

if [[ ! -s "$PRIVATE_KEY_FILE" ]]; then
    echo "Missing OCI API private key: $PRIVATE_KEY_FILE" >&2
    exit 5
fi
if ! grep -q 'BEGIN .*PRIVATE KEY' "$PRIVATE_KEY_FILE"; then
    echo "OCI API private key does not look like a PEM private key." >&2
    exit 6
fi

if [[ ! -s "$SSH_PUBLIC_KEY_FILE" ]]; then
    echo "Missing Katcha SSH public key: $SSH_PUBLIC_KEY_FILE" >&2
    exit 7
fi
SSH_PUBLIC_KEY="$(tr -d '\r\n' < "$SSH_PUBLIC_KEY_FILE")"
if [[ "$SSH_PUBLIC_KEY" != ssh-* ]]; then
    echo "Katcha SSH public key is not OpenSSH format." >&2
    exit 8
fi

OCI_TENANCY_OCID="${OCI_TENANCY_OCID:-}"
OCI_USER_OCID="${OCI_USER_OCID:-}"
OCI_FINGERPRINT="${OCI_FINGERPRINT:-}"

if [[ -z "$OCI_TENANCY_OCID" ]]; then
    read -r -p "OCI tenancy OCID: " OCI_TENANCY_OCID
fi
if [[ -z "$OCI_USER_OCID" ]]; then
    read -r -p "Dedicated OCI GitHub recovery user OCID: " OCI_USER_OCID
fi
if [[ -z "$OCI_FINGERPRINT" ]]; then
    read -r -p "OCI API key fingerprint: " OCI_FINGERPRINT
fi

if [[ "$OCI_TENANCY_OCID" != ocid1.tenancy.* ]]; then
    echo "Invalid tenancy OCID." >&2
    exit 9
fi
if [[ "$OCI_USER_OCID" != ocid1.user.* ]]; then
    echo "Invalid user OCID." >&2
    exit 10
fi
if [[ ! "$OCI_FINGERPRINT" =~ ^([0-9A-Fa-f]{2}:){15}[0-9A-Fa-f]{2}$ ]]; then
    echo "Invalid OCI API key fingerprint." >&2
    exit 11
fi

echo "Disabling bootstrap/recovery gates while configuration is written..."
gh variable set KATCHA_OCI_BOOTSTRAP_POLL_ENABLED --repo "$REPO" --body "false"
gh variable set KATCHA_OCI_RECOVERY_CONFIGURED --repo "$REPO" --body "false"
gh variable set KATCHA_OCI_PAID_FALLBACK_ENABLED --repo "$REPO" --body "false"
gh variable set KATCHA_EXTERNAL_COMPUTE_ENABLED --repo "$REPO" --body "false"

echo "Writing OCI bootstrap variables..."
gh variable set OCI_REGION --repo "$REPO" --body "us-ashburn-1"
gh variable set KATCHA_OCI_COMPARTMENT_ID --repo "$REPO" --body "ocid1.compartment.oc1..aaaaaaaa2qo6tzs6bya2t6xscavrqih6cqujawyh2tgsgfyo6iesuru6jvma"
gh variable set KATCHA_OCI_AVAILABILITY_DOMAIN --repo "$REPO" --body "OZDH:US-ASHBURN-AD-1"
gh variable set KATCHA_OCI_SUBNET_ID --repo "$REPO" --body "ocid1.subnet.oc1.iad.aaaaaaaaeef2nlnbkcytimuvhlbp425mzfigleg2kuievicpkxvtwmazi5vq"
gh variable set KATCHA_OCI_CROSS_AD_TARGETS_JSON --repo "$REPO" --body '[{"availability_domain":"OZDH:US-ASHBURN-AD-2","subnet_id":"ocid1.subnet.oc1.iad.aaaaaaaa3xmzxsnm3fvqgdruu64yhxg3xnhqihxbmcaaqyhmymvl5y6wn37a"},{"availability_domain":"OZDH:US-ASHBURN-AD-3","subnet_id":"ocid1.subnet.oc1.iad.aaaaaaaa6mkqi32nqmcz5uexfi2u3wawzhjeiacqbld56ooiks43xbvsxwiq"}]'
gh variable set KATCHA_OCI_PRIMARY_IMAGE_ID --repo "$REPO" --body "ocid1.image.oc1.iad.aaaaaaaa6o52ajhkewa2syfadrxel5b7tfwi5qdm45gl4ykwbqut25tvdxrq"
gh variable set KATCHA_OCI_PRIMARY_SHAPE --repo "$REPO" --body "VM.Standard.A1.Flex"
gh variable set KATCHA_OCI_PRIMARY_OCPUS --repo "$REPO" --body "2"
gh variable set KATCHA_OCI_PRIMARY_MEMORY_GB --repo "$REPO" --body "12"
gh variable set KATCHA_OCI_ASSIGN_PUBLIC_IP --repo "$REPO" --body "false"
gh variable set KATCHA_OCI_DATA_VOLUME_ID --repo "$REPO" --body "ocid1.volume.oc1.iad.abuwcljroxl5qmymubdwteawm5hgrsjp4fgjllxmatwdjibv3kfnghaaawrq"
gh variable set KATCHA_OCI_CROSS_AD_DATA_VOLUME_SIZE_GB --repo "$REPO" --body "50"
gh variable set KATCHA_OCI_DATA_VOLUME_DEVICE_PATH --repo "$REPO" --body "/dev/oracleoci/oraclevdb"
gh variable set KATCHA_OCI_SSH_PUBLIC_KEY --repo "$REPO" --body "$SSH_PUBLIC_KEY"

echo "Writing OCI API credentials as GitHub Actions secrets..."
printf '%s' "$OCI_TENANCY_OCID" | gh secret set OCI_TENANCY_OCID --repo "$REPO"
printf '%s' "$OCI_USER_OCID" | gh secret set OCI_USER_OCID --repo "$REPO"
printf '%s' "$OCI_FINGERPRINT" | gh secret set OCI_FINGERPRINT --repo "$REPO"
gh secret set OCI_API_PRIVATE_KEY --repo "$REPO" < "$PRIVATE_KEY_FILE"

echo
echo "Configured bootstrap variables:"
gh variable list --repo "$REPO" | grep -E '^(OCI_REGION|KATCHA_OCI_(BOOTSTRAP|RECOVERY_CONFIGURED|COMPARTMENT|AVAILABILITY|SUBNET|CROSS_AD|PRIMARY|ASSIGN_PUBLIC_IP|DATA_VOLUME|SSH_PUBLIC_KEY)|KATCHA_EXTERNAL_COMPUTE_ENABLED)' || true

echo
echo "Configured OCI secret names:"
gh secret list --repo "$REPO" | grep -E '^OCI_(TENANCY_OCID|USER_OCID|FINGERPRINT|API_PRIVATE_KEY)' || true

if [[ "$ENABLE" == "true" ]]; then
    required_transition_secrets=(
        KATCHA_GITHUB_AUTOMATION_TOKEN
        KATCHA_TELEGRAM_BOT_TOKEN
        KATCHA_TELEGRAM_CHAT_ID
    )
    configured_secret_names="$(
        gh secret list --repo "$REPO" --json name --jq '.[].name'
    )"
    for secret_name in "${required_transition_secrets[@]}"; do
        if ! grep -Fxq "$secret_name" <<<"$configured_secret_names"; then
            echo "Missing required transition secret: $secret_name" >&2
            echo "Polling will not be enabled until acquisition handoff/Telegram notification can complete." >&2
            exit 14
        fi
    done

    echo
    echo "Running one-shot OCI bootstrap validation before enabling the scheduler..."

    previous_run_id="$(
        gh run list \
            --repo "$REPO" \
            --workflow oci-bootstrap-capacity.yml \
            --event workflow_dispatch \
            --limit 1 \
            --json databaseId \
            --jq '.[0].databaseId // empty'
    )"

    gh workflow run oci-bootstrap-capacity.yml --repo "$REPO" -f mode=validate

    run_id=""
    for _ in {1..30}; do
        candidate="$(
            gh run list \
                --repo "$REPO" \
                --workflow oci-bootstrap-capacity.yml \
                --event workflow_dispatch \
                --limit 1 \
                --json databaseId \
                --jq '.[0].databaseId // empty'
        )"
        if [[ -n "$candidate" && "$candidate" != "$previous_run_id" ]]; then
            run_id="$candidate"
            break
        fi
        sleep 2
    done

    if [[ -z "$run_id" ]]; then
        echo "Could not identify the validation workflow run; polling remains disabled." >&2
        exit 12
    fi

    echo "Validation run: $run_id"
    if ! gh run watch "$run_id" --repo "$REPO" --exit-status; then
        echo
        echo "OCI bootstrap validation FAILED."
        echo "Scheduled polling remains disabled."
        exit 13
    fi

    echo
    echo "Validation succeeded. Enabling autonomous A1 bootstrap polling..."
    gh variable set KATCHA_OCI_BOOTSTRAP_POLL_ENABLED --repo "$REPO" --body "true"

    echo "Starting the first autonomous capacity pass..."
    gh workflow run oci-bootstrap-capacity.yml --repo "$REPO" --ref main -f mode=poll

    echo
    echo "Bootstrap polling is ENABLED."
    echo "Each completed AD1→AD2→AD3 miss immediately dispatches the next serialized pass."
    echo "The five-minute GitHub schedule remains only as a dead-man/backstop."
else
    echo
    echo "Configuration installed with polling DISABLED."
    echo "After validating the dedicated OCI identity and API key, rerun:"
    echo "  bash scripts/configure-github-oci-bootstrap.sh --enable"
fi
