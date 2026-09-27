#!/usr/bin/env bash
set -euo pipefail

MODE="plan"
APPROVED_SHA=""
case "${1:-}" in
    "")
        ;;
    --plan)
        MODE="plan"
        ;;
    --bootstrap-state)
        MODE="bootstrap-state"
        ;;
    --apply)
        MODE="apply"
        APPROVED_SHA="${2:-}"
        if [[ -z "${APPROVED_SHA}" || $# -ne 2 ]]; then
            echo "ERROR: --apply requires the exact reviewed plan SHA-256." >&2
            echo "Usage: bash scripts/aws_render_staging.sh --apply <plan-sha256>" >&2
            exit 2
        fi
        ;;
    --help|-h)
        cat <<'EOF'
Usage:
  bash scripts/aws_render_staging.sh
  bash scripts/aws_render_staging.sh --plan
  bash scripts/aws_render_staging.sh --bootstrap-state
  bash scripts/aws_render_staging.sh --apply <reviewed-plan-sha256>

Modes:
  plan (default)       No Terraform apply. Initializes the existing remote backend,
                       validates the root, writes a plan outside the repository,
                       shows it, and prints its SHA-256.
  --bootstrap-state    Explicitly creates/repairs only the guarded Terraform state bucket.
  --apply SHA256       Applies only the exact previously reviewed plan matching SHA256,
                       verifies outputs, and persists non-secret staging metadata.

Configuration is read from explicit environment first, then the ignored local .env.
Infrastructure writes use KATCHA_AWS_BOOTSTRAP_PROFILE (default: katcha).
Durable-role verification uses KATCHA_AWS_PROFILE (default: katcha-automation).
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
TF_DIR="${REPO_ROOT}/infra/aws-render-staging"

ENV_FILE_SETTING="${KATCHA_ENV_FILE:-.env}"
if [[ "${ENV_FILE_SETTING}" = /* ]]; then
    ENV_FILE="${ENV_FILE_SETTING}"
else
    ENV_FILE="${REPO_ROOT}/${ENV_FILE_SETTING}"
fi

if ! command -v python3 >/dev/null 2>&1; then
    echo "ERROR: required command is not installed: python3" >&2
    exit 2
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
BOOTSTRAP_PROFILE="${KATCHA_AWS_BOOTSTRAP_PROFILE:-katcha}"
DURABLE_PROFILE="${KATCHA_AWS_PROFILE:-$(read_env_key KATCHA_AWS_PROFILE)}"
RENDERER_ROLE="${KATCHA_AWS_AUTOMATION_ROLE:-KatchaChronosAutomation}"
STAGING_PREFIX="${KATCHA_REMOTION_STAGING_PREFIX:-$(read_env_key KATCHA_REMOTION_STAGING_PREFIX)}"
MAX_WAIT_MS="${KATCHA_REMOTION_LAMBDA_MAX_WAIT_MS:-$(read_env_key KATCHA_REMOTION_LAMBDA_MAX_WAIT_MS)}"
ROLES_DIR="${KATCHA_AWS_ROLES_ANYWHERE_DIR:-$HOME/.aws/katcha-roles-anywhere}"
PLAN_DIR="${KATCHA_AWS_TERRAFORM_PLAN_DIR:-${ROLES_DIR}/terraform-plans}"
PLAN_PATH="${KATCHA_AWS_STAGING_PLAN_PATH:-${PLAN_DIR}/aws-render-staging.tfplan}"
ENV_BACKUP_DIR="${ROLES_DIR}/env-backups"

REGION="${REGION:-us-east-1}"
DURABLE_PROFILE="${DURABLE_PROFILE:-katcha-automation}"
STAGING_PREFIX="${STAGING_PREFIX:-katcha-render-staging}"
MAX_WAIT_MS="${MAX_WAIT_MS:-1500000}"

if [[ ! "${EXPECTED_ACCOUNT}" =~ ^[0-9]{12}$ ]]; then
    echo "ERROR: KATCHA_AWS_EXPECTED_ACCOUNT_ID must be an explicit 12-digit AWS account ID." >&2
    exit 2
fi
if [[ -z "${REGION}" ]]; then
    echo "ERROR: KATCHA_REMOTION_LAMBDA_REGION must not be empty." >&2
    exit 2
fi
if [[ ! "${RENDERER_ROLE}" =~ ^[A-Za-z0-9+=,.@_-]{1,64}$ ]]; then
    echo "ERROR: KATCHA_AWS_AUTOMATION_ROLE is not a valid IAM role name." >&2
    exit 2
fi
if [[ ! "${MAX_WAIT_MS}" =~ ^[0-9]+$ ]] || (( MAX_WAIT_MS < 60000 )); then
    echo "ERROR: KATCHA_REMOTION_LAMBDA_MAX_WAIT_MS must be an integer >= 60000." >&2
    exit 2
fi
if [[ -z "${STAGING_PREFIX}" || "${STAGING_PREFIX}" == /* || "${STAGING_PREFIX}" == */ || "${STAGING_PREFIX}" == *//* ]]; then
    echo "ERROR: KATCHA_REMOTION_STAGING_PREFIX must be a normalized non-empty prefix." >&2
    exit 2
fi

for command in aws terraform python3 sha256sum mkdir chmod cp date dirname; do
    if ! command -v "${command}" >/dev/null 2>&1; then
        echo "ERROR: required command is not installed: ${command}" >&2
        exit 2
    fi
done

ACTUAL_ACCOUNT="$(
    aws sts get-caller-identity         --profile "${BOOTSTRAP_PROFILE}"         --region "${REGION}"         --query Account         --output text
)"
CALLER_ARN="$(
    aws sts get-caller-identity         --profile "${BOOTSTRAP_PROFILE}"         --region "${REGION}"         --query Arn         --output text
)"

if [[ "${ACTUAL_ACCOUNT}" != "${EXPECTED_ACCOUNT}" ]]; then
    echo "ERROR: AWS account mismatch." >&2
    echo "  expected: ${EXPECTED_ACCOUNT}" >&2
    echo "  actual:   ${ACTUAL_ACCOUNT}" >&2
    echo "  caller:   ${CALLER_ARN}" >&2
    exit 3
fi

STATE_BUCKET="katcha-tfstate-${EXPECTED_ACCOUNT}-${REGION}"
EXPECTED_STAGING_BUCKET="katcha-render-staging-${EXPECTED_ACCOUNT}-${REGION}"

echo "AWS infrastructure identity verified:"
echo "  account:          ${ACTUAL_ACCOUNT}"
echo "  caller:           ${CALLER_ARN}"
echo "  bootstrap profile:${BOOTSTRAP_PROFILE}"
echo "  region:           ${REGION}"
echo "  state bucket:     ${STATE_BUCKET}"
echo "  staging bucket:   ${EXPECTED_STAGING_BUCKET}"
echo "  staging prefix:   ${STAGING_PREFIX}"
echo "  renderer role:    ${RENDERER_ROLE}"
echo "  durable profile:  ${DURABLE_PROFILE}"
echo "  plan path:        ${PLAN_PATH}"

if [[ "${MODE}" == "bootstrap-state" ]]; then
    echo
    echo "Applying only the guarded Terraform state-bucket bootstrap..."
    AWS_PROFILE="${BOOTSTRAP_PROFILE}"     KATCHA_AWS_EXPECTED_ACCOUNT_ID="${EXPECTED_ACCOUNT}"     KATCHA_REMOTION_LAMBDA_REGION="${REGION}"     bash "${REPO_ROOT}/scripts/aws_tf_state_bootstrap.sh" --apply
    echo
    echo "PASS: Terraform remote state bootstrap completed."
    echo "No render-staging Terraform apply was performed."
    exit 0
fi

if ! aws s3api head-bucket     --profile "${BOOTSTRAP_PROFILE}"     --bucket "${STATE_BUCKET}" >/dev/null 2>&1; then
    echo "ERROR: Terraform state bucket is not reachable: ${STATE_BUCKET}" >&2
    echo "Run this explicit step first:" >&2
    echo "  bash scripts/aws_render_staging.sh --bootstrap-state" >&2
    exit 4
fi

mkdir -p "${PLAN_DIR}"
chmod 700 "${ROLES_DIR}" "${PLAN_DIR}" 2>/dev/null || true

init_backend() {
    (
        cd "${TF_DIR}"
        AWS_PROFILE="${BOOTSTRAP_PROFILE}" terraform init -reconfigure -input=false             -backend-config="bucket=${STATE_BUCKET}"             -backend-config="key=katcha/aws-render-staging/terraform.tfstate"             -backend-config="region=${REGION}"             -backend-config="encrypt=true"             -backend-config="use_lockfile=true"
    )
}

if [[ "${MODE}" == "plan" ]]; then
    echo
    echo "Initializing existing remote Terraform backend..."
    init_backend

    (
        cd "${TF_DIR}"
        terraform fmt -check
        terraform validate
    )

    echo
    echo "Creating reviewable staging plan..."
    (
        cd "${TF_DIR}"
        AWS_PROFILE="${BOOTSTRAP_PROFILE}" terraform plan             -input=false             -var="expected_account_id=${EXPECTED_ACCOUNT}"             -var="aws_region=${REGION}"             -var="renderer_role_name=${RENDERER_ROLE}"             -var="staging_prefix=${STAGING_PREFIX}"             -out="${PLAN_PATH}"
    )
    chmod 600 "${PLAN_PATH}"

    echo
    echo "----- BEGIN REVIEWABLE TERRAFORM PLAN -----"
    (
        cd "${TF_DIR}"
        terraform show -no-color "${PLAN_PATH}"
    )
    echo "----- END REVIEWABLE TERRAFORM PLAN -----"

    PLAN_SHA="$(sha256sum "${PLAN_PATH}" | awk '{print $1}')"
    echo
    echo "PLAN READY: no Terraform apply was performed."
    echo "  plan:   ${PLAN_PATH}"
    echo "  sha256: ${PLAN_SHA}"
    echo
    echo "After reviewing the plan above, apply only this exact plan with:"
    echo "  bash scripts/aws_render_staging.sh --apply ${PLAN_SHA}"
    exit 0
fi

if [[ ! "${APPROVED_SHA}" =~ ^[0-9a-fA-F]{64}$ ]]; then
    echo "ERROR: reviewed plan SHA-256 must be exactly 64 hexadecimal characters." >&2
    exit 5
fi
if [[ ! -f "${PLAN_PATH}" ]]; then
    echo "ERROR: reviewed Terraform plan does not exist: ${PLAN_PATH}" >&2
    echo "Run the default plan mode first." >&2
    exit 5
fi

ACTUAL_PLAN_SHA="$(sha256sum "${PLAN_PATH}" | awk '{print $1}')"
if [[ "${ACTUAL_PLAN_SHA,,}" != "${APPROVED_SHA,,}" ]]; then
    echo "ERROR: Terraform plan SHA-256 mismatch; refusing apply." >&2
    echo "  reviewed: ${APPROVED_SHA}" >&2
    echo "  actual:   ${ACTUAL_PLAN_SHA}" >&2
    exit 5
fi

echo
echo "Verified reviewed plan SHA-256: ${ACTUAL_PLAN_SHA}"
echo "Reinitializing the expected remote backend before apply..."
init_backend

echo
echo "Applying the exact reviewed Terraform plan..."
(
    cd "${TF_DIR}"
    AWS_PROFILE="${BOOTSTRAP_PROFILE}" terraform apply         -input=false         "${PLAN_PATH}"
)

BUCKET_NAME="$(
    cd "${TF_DIR}"
    AWS_PROFILE="${BOOTSTRAP_PROFILE}" terraform output -raw bucket_name
)"
OUTPUT_PREFIX="$(
    cd "${TF_DIR}"
    AWS_PROFILE="${BOOTSTRAP_PROFILE}" terraform output -raw staging_prefix
)"
ROLE_ARN="$(
    cd "${TF_DIR}"
    AWS_PROFILE="${BOOTSTRAP_PROFILE}" terraform output -raw renderer_role_arn
)"
POLICY_NAME="$(
    cd "${TF_DIR}"
    AWS_PROFILE="${BOOTSTRAP_PROFILE}" terraform output -raw renderer_staging_policy_name
)"

if [[ "${BUCKET_NAME}" != "${EXPECTED_STAGING_BUCKET}" ]]; then
    echo "ERROR: Terraform staging bucket output mismatch." >&2
    echo "  expected: ${EXPECTED_STAGING_BUCKET}" >&2
    echo "  actual:   ${BUCKET_NAME}" >&2
    exit 6
fi
if [[ "${OUTPUT_PREFIX}" != "${STAGING_PREFIX}" ]]; then
    echo "ERROR: Terraform staging prefix output mismatch." >&2
    echo "  expected: ${STAGING_PREFIX}" >&2
    echo "  actual:   ${OUTPUT_PREFIX}" >&2
    exit 6
fi
if [[ "${ROLE_ARN}" != "arn:aws:iam::${EXPECTED_ACCOUNT}:role/${RENDERER_ROLE}" ]]; then
    echo "ERROR: Terraform renderer role output mismatch: ${ROLE_ARN}" >&2
    exit 6
fi
if [[ "${POLICY_NAME}" != "KatchaRenderStagingAccess" ]]; then
    echo "ERROR: unexpected staging policy name: ${POLICY_NAME}" >&2
    exit 6
fi

echo
echo "Verifying the durable renderer role can read the staging bucket location..."
DURABLE_ACCOUNT="$(
    aws sts get-caller-identity         --profile "${DURABLE_PROFILE}"         --region "${REGION}"         --query Account         --output text
)"
if [[ "${DURABLE_ACCOUNT}" != "${EXPECTED_ACCOUNT}" ]]; then
    echo "ERROR: durable profile resolved to unexpected account: ${DURABLE_ACCOUNT}" >&2
    exit 7
fi

BUCKET_LOCATION="$(
    aws s3api get-bucket-location         --profile "${DURABLE_PROFILE}"         --bucket "${BUCKET_NAME}"         --query LocationConstraint         --output text
)"
if [[ "${BUCKET_LOCATION}" == "None" || "${BUCKET_LOCATION}" == "null" || -z "${BUCKET_LOCATION}" ]]; then
    BUCKET_LOCATION="us-east-1"
fi
if [[ "${BUCKET_LOCATION}" != "${REGION}" ]]; then
    echo "ERROR: durable role saw staging bucket in unexpected region: ${BUCKET_LOCATION}" >&2
    exit 7
fi

MAX_WAIT_SECONDS=$(( (MAX_WAIT_MS + 999) / 1000 ))
MIN_EXPIRY=$(( MAX_WAIT_SECONDS + 300 ))
URL_EXPIRY=3600
if (( MIN_EXPIRY > URL_EXPIRY )); then
    URL_EXPIRY="${MIN_EXPIRY}"
fi

echo
echo "Persisting verified non-secret staging metadata..."
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

python3 - "${ENV_FILE}"     "KATCHA_REMOTION_STAGING_BUCKET=${BUCKET_NAME}"     "KATCHA_REMOTION_STAGING_PREFIX=${OUTPUT_PREFIX}"     "KATCHA_REMOTION_STAGING_URL_EXPIRES_SECONDS=${URL_EXPIRY}" <<'PY'
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
    result.append("# Verified AWS render-staging metadata")
    result.extend(f"{key}={updates[key]}" for key in missing)

path.write_text("\n".join(result) + "\n", encoding="utf-8")
PY
chmod 600 "${ENV_FILE}"

BACKEND_VALUE="$(read_env_key KATCHA_RENDER_BACKEND)"

echo
echo "PASS: private render staging is provisioned and durable-role access is verified."
echo "  bucket:       ${BUCKET_NAME}"
echo "  prefix:       ${OUTPUT_PREFIX}"
echo "  region:       ${BUCKET_LOCATION}"
echo "  renderer role:${ROLE_ARN}"
echo "  policy:       ${POLICY_NAME}"
echo "  URL expiry:   ${URL_EXPIRY}s"
echo "  env file:     ${ENV_FILE}"
echo "  backend:      ${BACKEND_VALUE:-local} (unchanged)"
echo
echo "Lambda rendering remains disabled until the synthetic cloud-render acceptance passes."
