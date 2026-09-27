#!/usr/bin/env bash
set -euo pipefail

MODE="inspect"
case "${1:-}" in
    "")
        ;;
    --apply)
        MODE="apply"
        ;;
    --help|-h)
        cat <<'EOF'
Usage:
  bash scripts/aws_remotion_deploy.sh
  bash scripts/aws_remotion_deploy.sh --apply

Default mode is inspect-only and performs no Remotion/AWS deployment writes.

The helper uses the durable Katcha AWS profile, computes the exact function name
from the pinned Remotion package, deploys the function/site only with --apply,
verifies both, and persists only the verified deployment metadata into the ignored
local Katcha env file.

Required configuration (environment or local .env):
  KATCHA_AWS_EXPECTED_ACCOUNT_ID
  KATCHA_REMOTION_LAMBDA_REGION

Optional configuration:
  KATCHA_AWS_PROFILE              Default: katcha-automation
  KATCHA_ENV_FILE                 Default: .env
  KATCHA_REMOTION_SITE_NAME       Default: katcha-production
  KATCHA_AWS_ROLES_ANYWHERE_DIR   Default: ~/.aws/katcha-roles-anywhere
EOF
        exit 0
        ;;
    *)
        echo "ERROR: unsupported argument: $1" >&2
        exit 2
        ;;
esac

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd)"
RENDERER_DIR="${REPO_ROOT}/renderer"

ENV_FILE_SETTING="${KATCHA_ENV_FILE:-.env}"
if [[ "${ENV_FILE_SETTING}" = /* ]]; then
    ENV_FILE="${ENV_FILE_SETTING}"
else
    ENV_FILE="${REPO_ROOT}/${ENV_FILE_SETTING}"
fi

read_env_key() {
    local key="$1"
    python3 - "${ENV_FILE}" "${key}" <<'PY'
from pathlib import Path
import sys

path = Path(sys.argv[1])
key = sys.argv[2]
if not path.exists():
    raise SystemExit(0)

for raw in path.read_text(encoding="utf-8").splitlines():
    line = raw.strip()
    if not line or line.startswith("#") or "=" not in line:
        continue
    candidate, value = line.split("=", 1)
    if candidate.strip() != key:
        continue
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        value = value[1:-1]
    print(value)
    break
PY
}

EXPECTED_ACCOUNT="${KATCHA_AWS_EXPECTED_ACCOUNT_ID:-$(read_env_key KATCHA_AWS_EXPECTED_ACCOUNT_ID)}"
REGION="${KATCHA_REMOTION_LAMBDA_REGION:-$(read_env_key KATCHA_REMOTION_LAMBDA_REGION)}"
PROFILE="${KATCHA_AWS_PROFILE:-$(read_env_key KATCHA_AWS_PROFILE)}"
SITE_NAME="${KATCHA_REMOTION_SITE_NAME:-katcha-production}"
ROLES_DIR="${KATCHA_AWS_ROLES_ANYWHERE_DIR:-$HOME/.aws/katcha-roles-anywhere}"
ENV_BACKUP_DIR="${ROLES_DIR}/env-backups"

REGION="${REGION:-us-east-1}"
PROFILE="${PROFILE:-katcha-automation}"

FUNCTION_MEMORY_MB=4096
FUNCTION_DISK_MB=4096
FUNCTION_TIMEOUT_SECONDS=900
FUNCTION_RETENTION_DAYS=14

if [[ ! "${EXPECTED_ACCOUNT}" =~ ^[0-9]{12}$ ]]; then
    echo "ERROR: KATCHA_AWS_EXPECTED_ACCOUNT_ID must be an explicit 12-digit account ID." >&2
    exit 2
fi

if [[ -z "${REGION}" ]]; then
    echo "ERROR: KATCHA_REMOTION_LAMBDA_REGION must not be empty." >&2
    exit 2
fi

if [[ ! "${SITE_NAME}" =~ ^[A-Za-z0-9._-]{1,128}$ ]]; then
    echo "ERROR: KATCHA_REMOTION_SITE_NAME contains unsupported characters." >&2
    exit 2
fi

for command in aws node npx npm python3 cp date mkdir chmod; do
    if ! command -v "${command}" >/dev/null 2>&1; then
        echo "ERROR: required command is not installed: ${command}" >&2
        exit 2
    fi
done

if [[ ! -f "${RENDERER_DIR}/package.json" ]]; then
    echo "ERROR: renderer/package.json is missing." >&2
    exit 2
fi

FUNCTION_NAME="$(
    cd "${RENDERER_DIR}"
    node src/lambda-deployment-state.mjs function-name         "${FUNCTION_MEMORY_MB}"         "${FUNCTION_DISK_MB}"         "${FUNCTION_TIMEOUT_SECONDS}"
)"

if [[ ! "${FUNCTION_NAME}" =~ ^remotion-render-4-0-529-mem4096mb-disk4096mb-900sec$ ]]; then
    echo "ERROR: unexpected Remotion function name: ${FUNCTION_NAME}" >&2
    echo "The pinned deployment contract changed; do not deploy until reviewed." >&2
    exit 3
fi

ACTUAL_ACCOUNT="$(
    aws sts get-caller-identity         --profile "${PROFILE}"         --region "${REGION}"         --query Account         --output text
)"
CALLER_ARN="$(
    aws sts get-caller-identity         --profile "${PROFILE}"         --region "${REGION}"         --query Arn         --output text
)"

if [[ "${ACTUAL_ACCOUNT}" != "${EXPECTED_ACCOUNT}" ]]; then
    echo "ERROR: AWS account mismatch." >&2
    echo "  expected: ${EXPECTED_ACCOUNT}" >&2
    echo "  actual:   ${ACTUAL_ACCOUNT}" >&2
    echo "  caller:   ${CALLER_ARN}" >&2
    exit 4
fi

echo "AWS identity verified:"
echo "  account:       ${ACTUAL_ACCOUNT}"
echo "  caller:        ${CALLER_ARN}"
echo "  profile:       ${PROFILE}"
echo "  region:        ${REGION}"
echo
echo "Pinned Remotion deployment:"
echo "  function:      ${FUNCTION_NAME}"
echo "  memory:        ${FUNCTION_MEMORY_MB} MB"
echo "  disk:          ${FUNCTION_DISK_MB} MB"
echo "  timeout:       ${FUNCTION_TIMEOUT_SECONDS}s"
echo "  retention:     ${FUNCTION_RETENTION_DAYS} days"
echo "  site:          ${SITE_NAME}"
echo "  entry point:   renderer/src/entry.jsx"
echo "  runtime env:   ${ENV_FILE}"

if aws lambda get-function     --profile "${PROFILE}"     --region "${REGION}"     --function-name "${FUNCTION_NAME}" >/dev/null 2>&1; then
    echo "  function state: already deployed"
else
    echo "  function state: not currently found"
fi

set +e
EXISTING_SITE_JSON="$(
    cd "${RENDERER_DIR}"
    AWS_PROFILE="${PROFILE}"     REMOTION_AWS_PROFILE="${PROFILE}"     node src/lambda-deployment-state.mjs site "${REGION}" "${SITE_NAME}" 2>/dev/null
)"
SITE_LOOKUP_RC=$?
set -e

if [[ "${SITE_LOOKUP_RC}" -eq 0 ]]; then
    EXISTING_SERVE_URL="$(
        python3 -c 'import json,sys; print(json.load(sys.stdin)["serveUrl"])'             <<<"${EXISTING_SITE_JSON}"
    )"
    echo "  site state:     already deployed"
    echo "  serve URL:      ${EXISTING_SERVE_URL}"
else
    echo "  site state:     not currently found"
fi

if [[ "${MODE}" == "inspect" ]]; then
    echo
    echo "INSPECT ONLY: no Lambda function or Remotion site was deployed or modified."
    echo "Run again with --apply only after the account and pinned deployment above are correct."
    exit 0
fi

echo
echo "Running no-write Remotion permission/quota preflight with the durable profile..."
AWS_PROFILE="${PROFILE}" REMOTION_AWS_PROFILE="${PROFILE}" KATCHA_AWS_EXPECTED_ACCOUNT_ID="${EXPECTED_ACCOUNT}" KATCHA_REMOTION_LAMBDA_REGION="${REGION}" bash "${REPO_ROOT}/scripts/aws_render_preflight.sh"

echo
echo "Deploying pinned Remotion Lambda function..."
(
    cd "${RENDERER_DIR}"
    AWS_PROFILE="${PROFILE}"     REMOTION_AWS_PROFILE="${PROFILE}"     npx remotion lambda functions deploy         --region="${REGION}"         --memory="${FUNCTION_MEMORY_MB}"         --disk="${FUNCTION_DISK_MB}"         --timeout="${FUNCTION_TIMEOUT_SECONDS}"         --retention-period="${FUNCTION_RETENTION_DAYS}"
)

DEPLOYED_FUNCTION="$(
    aws lambda get-function         --profile "${PROFILE}"         --region "${REGION}"         --function-name "${FUNCTION_NAME}"         --query Configuration.FunctionName         --output text
)"
if [[ "${DEPLOYED_FUNCTION}" != "${FUNCTION_NAME}" ]]; then
    echo "ERROR: deployed function verification failed." >&2
    echo "  expected: ${FUNCTION_NAME}" >&2
    echo "  actual:   ${DEPLOYED_FUNCTION}" >&2
    exit 5
fi

echo
echo "Deploying Katcha Remotion site..."
(
    cd "${RENDERER_DIR}"
    AWS_PROFILE="${PROFILE}"     REMOTION_AWS_PROFILE="${PROFILE}"     npx remotion lambda sites create src/entry.jsx         --region="${REGION}"         --site-name="${SITE_NAME}"
)

SITE_JSON="$(
    cd "${RENDERER_DIR}"
    AWS_PROFILE="${PROFILE}"     REMOTION_AWS_PROFILE="${PROFILE}"     node src/lambda-deployment-state.mjs site "${REGION}" "${SITE_NAME}"
)"

SERVE_URL="$(
    python3 - "${SITE_NAME}" <<'PY' <<<"${SITE_JSON}"
import json
import sys
from urllib.parse import urlparse

site_name = sys.argv[1]
site = json.load(sys.stdin)

if site.get("id") != site_name:
    raise SystemExit(f'expected site id {site_name!r}, got {site.get("id")!r}')

serve_url = str(site.get("serveUrl") or "")
parsed = urlparse(serve_url)
if parsed.scheme != "https" or not parsed.netloc:
    raise SystemExit(f"site serveUrl is not a valid HTTPS URL: {serve_url!r}")

print(serve_url)
PY
)"

echo
echo "Verifying deployed function/site composition access..."
(
    cd "${RENDERER_DIR}"
    AWS_PROFILE="${PROFILE}"     REMOTION_AWS_PROFILE="${PROFILE}"     npx remotion lambda compositions "${SERVE_URL}"         --region="${REGION}"         --function-name="${FUNCTION_NAME}"         --quiet
)

echo
echo "Persisting verified deployment metadata without enabling Lambda rendering..."
mkdir -p "${ENV_BACKUP_DIR}"
chmod 700 "${ROLES_DIR}" "${ENV_BACKUP_DIR}" 2>/dev/null || true

env_existed=false
if [[ -f "${ENV_FILE}" ]]; then
    env_existed=true
elif [[ "${ENV_FILE}" == "${REPO_ROOT}/.env" && -f "${REPO_ROOT}/.env.example" ]]; then
    cp "${REPO_ROOT}/.env.example" "${ENV_FILE}"
else
    mkdir -p "$(dirname "${ENV_FILE}")"
    : >"${ENV_FILE}"
fi

if [[ "${env_existed}" == "true" ]]; then
    env_backup="${ENV_BACKUP_DIR}/katcha.env.$(date +%Y%m%d%H%M%S).bak"
    cp -a "${ENV_FILE}" "${env_backup}"
    chmod 600 "${env_backup}"
    echo "Backed up Katcha env file outside the repository: ${env_backup}"
fi

python3 - "${ENV_FILE}"     "KATCHA_REMOTION_LAMBDA_FUNCTION_NAME=${FUNCTION_NAME}"     "KATCHA_REMOTION_LAMBDA_SERVE_URL=${SERVE_URL}" <<'PY'
from pathlib import Path
import sys

path = Path(sys.argv[1])
updates = dict(item.split("=", 1) for item in sys.argv[2:])
lines = path.read_text(encoding="utf-8").splitlines()
seen = set()
result = []

for line in lines:
    stripped = line.lstrip()
    replaced = False
    if stripped and not stripped.startswith("#") and "=" in line:
        key = line.split("=", 1)[0].strip()
        if key in updates:
            result.append(f"{key}={updates[key]}")
            seen.add(key)
            replaced = True
    if not replaced:
        result.append(line)

missing = [key for key in updates if key not in seen]
if missing:
    if result and result[-1] != "":
        result.append("")
    result.append("# Verified Remotion Lambda deployment metadata")
    result.extend(f"{key}={updates[key]}" for key in missing)

path.write_text("\n".join(result) + "\n", encoding="utf-8")
PY

chmod 600 "${ENV_FILE}"

BACKEND_VALUE="$(read_env_key KATCHA_RENDER_BACKEND)"
echo
echo "PASS: Remotion Lambda function and site are deployed and verified."
echo "  function:   ${FUNCTION_NAME}"
echo "  serve URL:  ${SERVE_URL}"
echo "  env file:   ${ENV_FILE}"
echo "  backend:    ${BACKEND_VALUE:-local} (unchanged)"
echo
echo "Lambda rendering remains disabled until private staging and cloud-render acceptance pass."
