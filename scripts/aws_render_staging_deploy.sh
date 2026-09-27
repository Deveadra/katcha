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
  bash scripts/aws_render_staging_deploy.sh
  bash scripts/aws_render_staging_deploy.sh --apply

Default mode initializes the remote backend, validates Terraform, and produces a
reviewable plan without applying it. --apply applies that exact plan, verifies the
AWS resources, and persists only verified staging metadata into the local Katcha
env file.

Required configuration (environment or local .env):
  KATCHA_AWS_EXPECTED_ACCOUNT_ID

Optional configuration:
  KATCHA_REMOTION_LAMBDA_REGION       Default: us-east-1
  KATCHA_AWS_BOOTSTRAP_PROFILE        Default: katcha
  KATCHA_AWS_AUTOMATION_ROLE          Default: KatchaChronosAutomation
  KATCHA_TERRAFORM_STATE_BUCKET       Default: katcha-tfstate-<account>-<region>
  KATCHA_REMOTION_STAGING_BUCKET      Optional explicit bucket name
  KATCHA_REMOTION_STAGING_PREFIX      Default: katcha-render-staging
  KATCHA_ENV_FILE                     Default: .env
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
AUTOMATION_ROLE="${KATCHA_AWS_AUTOMATION_ROLE:-KatchaChronosAutomation}"
STAGING_BUCKET="${KATCHA_REMOTION_STAGING_BUCKET:-$(read_env_key KATCHA_REMOTION_STAGING_BUCKET)}"
STAGING_PREFIX="${KATCHA_REMOTION_STAGING_PREFIX:-$(read_env_key KATCHA_REMOTION_STAGING_PREFIX)}"

REGION="${REGION:-us-east-1}"
STAGING_PREFIX="${STAGING_PREFIX:-katcha-render-staging}"
STATE_BUCKET="${KATCHA_TERRAFORM_STATE_BUCKET:-katcha-tfstate-${EXPECTED_ACCOUNT}-${REGION}}"
ROLES_DIR="${KATCHA_AWS_ROLES_ANYWHERE_DIR:-$HOME/.aws/katcha-roles-anywhere}"
ENV_BACKUP_DIR="${ROLES_DIR}/env-backups"
POLICY_NAME="KatchaRenderStagingAccess"
PLAN_FILE="${TMPDIR:-/tmp}/katcha-render-staging-$$.tfplan"

trap 'rm -f "${PLAN_FILE}"' EXIT

if [[ ! "${EXPECTED_ACCOUNT}" =~ ^[0-9]{12}$ ]]; then
    echo "ERROR: KATCHA_AWS_EXPECTED_ACCOUNT_ID must be an explicit 12-digit AWS account ID." >&2
    exit 2
fi
if [[ -z "${REGION}" ]]; then
    echo "ERROR: KATCHA_REMOTION_LAMBDA_REGION must not be empty." >&2
    exit 2
fi
if [[ ! "${AUTOMATION_ROLE}" =~ ^[A-Za-z0-9+=,.@_-]{1,64}$ ]]; then
    echo "ERROR: KATCHA_AWS_AUTOMATION_ROLE is not a valid IAM role name." >&2
    exit 2
fi
if [[ -z "${STAGING_PREFIX}" || "${STAGING_PREFIX}" == /* || "${STAGING_PREFIX}" == */ || "${STAGING_PREFIX}" == *//* ]]; then
    echo "ERROR: KATCHA_REMOTION_STAGING_PREFIX must be a normalized non-empty prefix." >&2
    exit 2
fi

for command in aws terraform python3 cp date mkdir chmod dirname; do
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

if ! aws s3api head-bucket     --profile "${BOOTSTRAP_PROFILE}"     --region "${REGION}"     --bucket "${STATE_BUCKET}" >/dev/null 2>&1; then
    echo "ERROR: Terraform state bucket is missing or unreachable: ${STATE_BUCKET}" >&2
    echo "Run scripts/aws_tf_state_bootstrap.sh first." >&2
    exit 4
fi

STATE_REGION="$(
    aws s3api get-bucket-location         --profile "${BOOTSTRAP_PROFILE}"         --region "${REGION}"         --bucket "${STATE_BUCKET}"         --query LocationConstraint         --output text
)"
if [[ "${STATE_REGION}" == "None" || "${STATE_REGION}" == "null" || -z "${STATE_REGION}" ]]; then
    STATE_REGION="us-east-1"
fi
if [[ "${STATE_REGION}" != "${REGION}" ]]; then
    echo "ERROR: Terraform state bucket region mismatch: expected ${REGION}, got ${STATE_REGION}" >&2
    exit 4
fi

echo "AWS identity verified:"
echo "  account:        ${ACTUAL_ACCOUNT}"
echo "  caller:         ${CALLER_ARN}"
echo "  profile:        ${BOOTSTRAP_PROFILE}"
echo "  region:         ${REGION}"
echo "  state bucket:   ${STATE_BUCKET}"
echo "  renderer role:  ${AUTOMATION_ROLE}"
echo "  staging prefix: ${STAGING_PREFIX}"
if [[ -n "${STAGING_BUCKET}" ]]; then
    echo "  staging bucket: ${STAGING_BUCKET}"
else
    echo "  staging bucket: katcha-render-staging-${EXPECTED_ACCOUNT}-${REGION} (default)"
fi

export AWS_PROFILE="${BOOTSTRAP_PROFILE}"
export AWS_REGION="${REGION}"
export AWS_DEFAULT_REGION="${REGION}"

terraform -chdir="${TF_DIR}" init -reconfigure -input=false     -backend-config="bucket=${STATE_BUCKET}"     -backend-config="key=katcha/aws-render-staging/terraform.tfstate"     -backend-config="region=${REGION}"     -backend-config="encrypt=true"     -backend-config="use_lockfile=true" >/dev/null

terraform -chdir="${TF_DIR}" fmt -check
terraform -chdir="${TF_DIR}" validate

PLAN_ARGS=(
    -input=false
    "-var=expected_account_id=${EXPECTED_ACCOUNT}"
    "-var=aws_region=${REGION}"
    "-var=renderer_role_name=${AUTOMATION_ROLE}"
    "-var=staging_prefix=${STAGING_PREFIX}"
    "-out=${PLAN_FILE}"
)
if [[ -n "${STAGING_BUCKET}" ]]; then
    PLAN_ARGS+=("-var=bucket_name=${STAGING_BUCKET}")
fi
if [[ "${MODE}" == "inspect" ]]; then
    PLAN_ARGS+=("-lock=false")
fi

terraform -chdir="${TF_DIR}" plan "${PLAN_ARGS[@]}"

echo
echo "Terraform plan:"
terraform -chdir="${TF_DIR}" show -no-color "${PLAN_FILE}"

if [[ "${MODE}" == "inspect" ]]; then
    echo
    echo "INSPECT ONLY: no staging resources were applied."
    echo "Run again with --apply only after reviewing the plan above."
    exit 0
fi

echo
echo "Applying exact reviewed staging plan..."
terraform -chdir="${TF_DIR}" apply -input=false -auto-approve "${PLAN_FILE}"

BUCKET="$(terraform -chdir="${TF_DIR}" output -raw bucket_name)"
PREFIX="$(terraform -chdir="${TF_DIR}" output -raw staging_prefix)"
ROLE_ARN="$(terraform -chdir="${TF_DIR}" output -raw renderer_role_arn)"
ATTACHED_POLICY_NAME="$(terraform -chdir="${TF_DIR}" output -raw renderer_staging_policy_name)"
EXPECTED_POLICY="$(terraform -chdir="${TF_DIR}" output -raw renderer_iam_policy_json)"

if [[ "${ROLE_ARN}" != "arn:aws:iam::${EXPECTED_ACCOUNT}:role/${AUTOMATION_ROLE}" ]]; then
    echo "ERROR: Terraform resolved an unexpected renderer role ARN: ${ROLE_ARN}" >&2
    exit 5
fi
if [[ "${ATTACHED_POLICY_NAME}" != "${POLICY_NAME}" ]]; then
    echo "ERROR: unexpected staging policy name: ${ATTACHED_POLICY_NAME}" >&2
    exit 5
fi
if [[ "${PREFIX}" != "${STAGING_PREFIX}" ]]; then
    echo "ERROR: Terraform staging prefix mismatch: expected ${STAGING_PREFIX}, got ${PREFIX}" >&2
    exit 5
fi

BUCKET_REGION="$(
    aws s3api get-bucket-location         --profile "${BOOTSTRAP_PROFILE}"         --region "${REGION}"         --bucket "${BUCKET}"         --query LocationConstraint         --output text
)"
if [[ "${BUCKET_REGION}" == "None" || "${BUCKET_REGION}" == "null" || -z "${BUCKET_REGION}" ]]; then
    BUCKET_REGION="us-east-1"
fi
if [[ "${BUCKET_REGION}" != "${REGION}" ]]; then
    echo "ERROR: staging bucket region mismatch." >&2
    exit 6
fi

PUBLIC_BLOCK="$(
    aws s3api get-public-access-block         --profile "${BOOTSTRAP_PROFILE}"         --region "${REGION}"         --bucket "${BUCKET}"         --output json
)"
OWNERSHIP="$(
    aws s3api get-bucket-ownership-controls         --profile "${BOOTSTRAP_PROFILE}"         --region "${REGION}"         --bucket "${BUCKET}"         --query 'OwnershipControls.Rules[0].ObjectOwnership'         --output text
)"
ENCRYPTION="$(
    aws s3api get-bucket-encryption         --profile "${BOOTSTRAP_PROFILE}"         --region "${REGION}"         --bucket "${BUCKET}"         --query 'ServerSideEncryptionConfiguration.Rules[0].ApplyServerSideEncryptionByDefault.SSEAlgorithm'         --output text
)"
LIFECYCLE="$(
    aws s3api get-bucket-lifecycle-configuration         --profile "${BOOTSTRAP_PROFILE}"         --region "${REGION}"         --bucket "${BUCKET}"         --output json
)"
BUCKET_POLICY="$(
    aws s3api get-bucket-policy         --profile "${BOOTSTRAP_PROFILE}"         --region "${REGION}"         --bucket "${BUCKET}"         --query Policy         --output text
)"
ACTUAL_POLICY="$(
    aws iam get-role-policy         --profile "${BOOTSTRAP_PROFILE}"         --role-name "${AUTOMATION_ROLE}"         --policy-name "${POLICY_NAME}"         --query PolicyDocument         --output json
)"

PUBLIC_BLOCK="${PUBLIC_BLOCK}" LIFECYCLE="${LIFECYCLE}" BUCKET_POLICY="${BUCKET_POLICY}" EXPECTED_POLICY="${EXPECTED_POLICY}" ACTUAL_POLICY="${ACTUAL_POLICY}" PREFIX="${PREFIX}" python3 - <<'PY'
import json
import os
from urllib.parse import unquote

public = json.loads(os.environ["PUBLIC_BLOCK"]).get("PublicAccessBlockConfiguration", {})
required = ("BlockPublicAcls", "IgnorePublicAcls", "BlockPublicPolicy", "RestrictPublicBuckets")
if not all(public.get(key) is True for key in required):
    raise SystemExit("ERROR: staging bucket public access block is not fully enabled")

lifecycle = json.loads(os.environ["LIFECYCLE"])
prefix = os.environ["PREFIX"].rstrip("/") + "/"
matching = [
    rule for rule in lifecycle.get("Rules", [])
    if rule.get("Status") == "Enabled"
    and (rule.get("Filter") or {}).get("Prefix") == prefix
    and int((rule.get("Expiration") or {}).get("Days") or 0) >= 1
]
if not matching:
    raise SystemExit("ERROR: staging lifecycle expiration rule is missing or invalid")

raw_policy = os.environ["BUCKET_POLICY"]
try:
    bucket_policy = json.loads(raw_policy)
except json.JSONDecodeError:
    bucket_policy = json.loads(unquote(raw_policy))
secure = False
for statement in bucket_policy.get("Statement", []):
    condition = statement.get("Condition", {})
    if (
        statement.get("Effect") == "Deny"
        and statement.get("Action") == "s3:*"
        and str(condition.get("Bool", {}).get("aws:SecureTransport")).lower() == "false"
    ):
        secure = True
        break
if not secure:
    raise SystemExit("ERROR: HTTPS-only staging bucket policy was not verified")

expected = json.loads(os.environ["EXPECTED_POLICY"])
actual = json.loads(os.environ["ACTUAL_POLICY"])
if expected != actual:
    raise SystemExit("ERROR: attached KatchaRenderStagingAccess policy differs from Terraform output")
PY

if [[ "${OWNERSHIP}" != "BucketOwnerEnforced" ]]; then
    echo "ERROR: staging bucket ownership is not BucketOwnerEnforced: ${OWNERSHIP}" >&2
    exit 6
fi
if [[ "${ENCRYPTION}" != "AES256" ]]; then
    echo "ERROR: staging bucket encryption is not AES256: ${ENCRYPTION}" >&2
    exit 6
fi

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

python3 - "${ENV_FILE}"     "KATCHA_REMOTION_STAGING_BUCKET=${BUCKET}"     "KATCHA_REMOTION_STAGING_PREFIX=${PREFIX}" <<'PY'
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

BACKEND="$(read_env_key KATCHA_RENDER_BACKEND)"
echo
echo "PASS: private render-staging infrastructure is applied and verified."
echo "  bucket:       ${BUCKET}"
echo "  region:       ${BUCKET_REGION}"
echo "  prefix:       ${PREFIX}"
echo "  role:         ${ROLE_ARN}"
echo "  policy:       ${POLICY_NAME}"
echo "  public:       blocked"
echo "  ownership:    ${OWNERSHIP}"
echo "  encryption:   ${ENCRYPTION}"
echo "  runtime env:  ${ENV_FILE}"
echo "  backend:      ${BACKEND:-local} (unchanged)"
echo
echo "Next gate: run a synthetic render through the normal Katcha HTTP renderer with"
echo "KATCHA_RENDER_BACKEND=lambda only after the function/site metadata is verified."
