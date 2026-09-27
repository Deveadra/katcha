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
  bash scripts/aws_lambda_backend_enable.sh
  bash scripts/aws_lambda_backend_enable.sh --apply

Default mode verifies the latest controlled Lambda acceptance receipt against the
current repository commit, AWS account, Remotion deployment, and staging metadata.
It performs no writes.

--apply changes only KATCHA_RENDER_BACKEND=lambda in the ignored local Katcha env
after all receipt and AWS verification succeeds.

Optional:
  KATCHA_LAMBDA_ACCEPTANCE_MAX_AGE_SECONDS   Default: 86400 (24 hours)
  KATCHA_LAMBDA_ACCEPTANCE_RECEIPT          Override acceptance receipt path
  KATCHA_ENV_FILE                           Default: .env
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
FUNCTION_NAME="${KATCHA_REMOTION_LAMBDA_FUNCTION_NAME:-$(read_env_key KATCHA_REMOTION_LAMBDA_FUNCTION_NAME)}"
SERVE_URL="${KATCHA_REMOTION_LAMBDA_SERVE_URL:-$(read_env_key KATCHA_REMOTION_LAMBDA_SERVE_URL)}"
STAGING_BUCKET="${KATCHA_REMOTION_STAGING_BUCKET:-$(read_env_key KATCHA_REMOTION_STAGING_BUCKET)}"
STAGING_PREFIX="${KATCHA_REMOTION_STAGING_PREFIX:-$(read_env_key KATCHA_REMOTION_STAGING_PREFIX)}"
PROFILE="${PROFILE:-katcha-automation}"
REGION="${REGION:-us-east-1}"
STAGING_PREFIX="${STAGING_PREFIX:-katcha-render-staging}"

ROLES_DIR="${KATCHA_AWS_ROLES_ANYWHERE_DIR:-$HOME/.aws/katcha-roles-anywhere}"
RECEIPT_FILE="${KATCHA_LAMBDA_ACCEPTANCE_RECEIPT:-${ROLES_DIR}/acceptance/latest.json}"
ENV_BACKUP_DIR="${ROLES_DIR}/env-backups"
MAX_AGE="${KATCHA_LAMBDA_ACCEPTANCE_MAX_AGE_SECONDS:-86400}"

[[ "${EXPECTED_ACCOUNT}" =~ ^[0-9]{12}$ ]] || {
    echo "ERROR: KATCHA_AWS_EXPECTED_ACCOUNT_ID is missing or invalid." >&2
    exit 2
}
[[ -n "${FUNCTION_NAME}" ]] || {
    echo "ERROR: verified Remotion Lambda function metadata is missing." >&2
    exit 2
}
[[ "${SERVE_URL}" == https://* ]] || {
    echo "ERROR: verified Remotion Serve URL is missing or invalid." >&2
    exit 2
}
[[ -n "${STAGING_BUCKET}" ]] || {
    echo "ERROR: verified staging bucket metadata is missing." >&2
    exit 2
}
[[ "${MAX_AGE}" =~ ^[0-9]+$ && "${MAX_AGE}" -ge 300 ]] || {
    echo "ERROR: KATCHA_LAMBDA_ACCEPTANCE_MAX_AGE_SECONDS must be an integer >= 300." >&2
    exit 2
}
[[ -f "${RECEIPT_FILE}" ]] || {
    echo "ERROR: Lambda acceptance receipt not found: ${RECEIPT_FILE}" >&2
    echo "Run scripts/aws_lambda_render_acceptance.sh --run first." >&2
    exit 3
}

for command in aws python3 git cp date mkdir chmod; do
    command -v "${command}" >/dev/null 2>&1 || {
        echo "ERROR: required command is not installed: ${command}" >&2
        exit 2
    }
done

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
    exit 4
fi

DEPLOYED_FUNCTION="$(
    aws lambda get-function         --profile "${PROFILE}"         --region "${REGION}"         --function-name "${FUNCTION_NAME}"         --query Configuration.FunctionName         --output text
)"
[[ "${DEPLOYED_FUNCTION}" == "${FUNCTION_NAME}" ]] || {
    echo "ERROR: configured Remotion function could not be verified." >&2
    exit 4
}

BUCKET_REGION="$(
    aws s3api get-bucket-location         --profile "${PROFILE}"         --region "${REGION}"         --bucket "${STAGING_BUCKET}"         --query LocationConstraint         --output text
)"
if [[ "${BUCKET_REGION}" == "None" || "${BUCKET_REGION}" == "null" || -z "${BUCKET_REGION}" ]]; then
    BUCKET_REGION="us-east-1"
fi
[[ "${BUCKET_REGION}" == "${REGION}" ]] || {
    echo "ERROR: staging bucket region mismatch." >&2
    exit 4
}

GIT_COMMIT="$(git -C "${REPO_ROOT}" rev-parse HEAD)"

RECEIPT_SUMMARY="$(
    python3 - "${RECEIPT_FILE}" "${EXPECTED_ACCOUNT}" "${REGION}" "${PROFILE}"         "${FUNCTION_NAME}" "${SERVE_URL}" "${STAGING_BUCKET}" "${STAGING_PREFIX}"         "${GIT_COMMIT}" "${MAX_AGE}" <<'PY'
from datetime import datetime, timezone
import json
from pathlib import Path
import sys

(
    receipt_path,
    account_id,
    region,
    profile,
    function_name,
    serve_url,
    staging_bucket,
    staging_prefix,
    git_commit,
    max_age_raw,
) = sys.argv[1:]

receipt = json.loads(Path(receipt_path).read_text(encoding="utf-8"))
if receipt.get("version") != 1:
    raise SystemExit("ERROR: unsupported Lambda acceptance receipt version")

expected = {
    "account_id": account_id,
    "region": region,
    "aws_profile": profile,
    "function_name": function_name,
    "serve_url": serve_url,
    "staging_bucket": staging_bucket,
    "staging_prefix": staging_prefix,
    "git_commit": git_commit,
}
for key, value in expected.items():
    actual = str(receipt.get(key) or "")
    if actual != value:
        raise SystemExit(
            f"ERROR: acceptance receipt mismatch for {key}: expected {value!r}, got {actual!r}"
        )

if receipt.get("renderer") != "remotion-lambda":
    raise SystemExit("ERROR: acceptance receipt did not use remotion-lambda")
if receipt.get("verified") is not True or receipt.get("reused") is not False:
    raise SystemExit("ERROR: acceptance receipt is not a fresh verified render")
if not str(receipt.get("render_id") or "").strip():
    raise SystemExit("ERROR: acceptance receipt is missing render_id")
if int(receipt.get("lambdas_invoked") or 0) <= 0:
    raise SystemExit("ERROR: acceptance receipt has no Lambda invocations")
if int(receipt.get("object_size_bytes") or 0) <= 0:
    raise SystemExit("ERROR: acceptance receipt has an empty output object")
if not str(receipt.get("output_key") or "").strip():
    raise SystemExit("ERROR: acceptance receipt is missing output_key")

raw_time = str(receipt.get("accepted_at_utc") or "")
try:
    accepted = datetime.fromisoformat(raw_time.replace("Z", "+00:00"))
except ValueError as exc:
    raise SystemExit("ERROR: acceptance receipt timestamp is invalid") from exc
if accepted.tzinfo is None:
    accepted = accepted.replace(tzinfo=timezone.utc)

now = datetime.now(timezone.utc)
age = (now - accepted.astimezone(timezone.utc)).total_seconds()
if age < -300:
    raise SystemExit("ERROR: acceptance receipt timestamp is unexpectedly in the future")
max_age = int(max_age_raw)
if age > max_age:
    raise SystemExit(
        f"ERROR: acceptance receipt is stale: age={int(age)}s max={max_age}s"
    )

print(
    f"run_id={receipt.get('run_id')} "
    f"render_id={receipt.get('render_id')} "
    f"output_key={receipt.get('output_key')} "
    f"age_seconds={max(0, int(age))}"
)
PY
)"

echo "Lambda backend enablement gate verified:"
echo "  account:  ${ACTUAL_ACCOUNT}"
echo "  caller:   ${CALLER_ARN}"
echo "  profile:  ${PROFILE}"
echo "  region:   ${REGION}"
echo "  function: ${FUNCTION_NAME}"
echo "  site:     ${SERVE_URL}"
echo "  staging:  s3://${STAGING_BUCKET}/${STAGING_PREFIX}/"
echo "  commit:   ${GIT_COMMIT}"
echo "  receipt:  ${RECEIPT_FILE}"
echo "  result:   ${RECEIPT_SUMMARY}"

if [[ "${MODE}" == "inspect" ]]; then
    echo "INSPECT ONLY: KATCHA_RENDER_BACKEND was not changed."
    exit 0
fi

mkdir -p "${ENV_BACKUP_DIR}"
chmod 700 "${ROLES_DIR}" "${ENV_BACKUP_DIR}" 2>/dev/null || true

if [[ -f "${ENV_FILE}" ]]; then
    backup="${ENV_BACKUP_DIR}/katcha.env.$(date +%Y%m%d%H%M%S).bak"
    cp -a "${ENV_FILE}" "${backup}"
    chmod 600 "${backup}"
    echo "Backed up Katcha env file outside the repository: ${backup}"
else
    echo "ERROR: Katcha env file does not exist: ${ENV_FILE}" >&2
    exit 5
fi

python3 - "${ENV_FILE}" <<'PY'
from pathlib import Path
import sys

path = Path(sys.argv[1])
lines = path.read_text(encoding="utf-8").splitlines()
result = []
replaced = False

for line in lines:
    stripped = line.lstrip()
    if stripped and not stripped.startswith("#") and "=" in line:
        key = line.split("=", 1)[0].strip()
        if key == "KATCHA_RENDER_BACKEND":
            result.append("KATCHA_RENDER_BACKEND=lambda")
            replaced = True
            continue
    result.append(line)

if not replaced:
    if result and result[-1] != "":
        result.append("")
    result.append("KATCHA_RENDER_BACKEND=lambda")

path.write_text("\n".join(result) + "\n", encoding="utf-8")
PY
chmod 600 "${ENV_FILE}"

BACKEND="$(read_env_key KATCHA_RENDER_BACKEND)"
[[ "${BACKEND}" == "lambda" ]] || {
    echo "ERROR: backend persistence verification failed." >&2
    exit 6
}

echo
echo "PASS: Katcha Lambda rendering is now enabled in ${ENV_FILE}."
echo "Rollback is non-destructive: set KATCHA_RENDER_BACKEND=local and recreate only the renderer."
