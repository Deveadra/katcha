#!/usr/bin/env bash
set -euo pipefail

MODE="inspect"
case "${1:-}" in
    "") ;;
    --run) MODE="run" ;;
    --help|-h)
        cat <<'EOF'
Usage:
  bash scripts/aws_lambda_render_acceptance.sh
  bash scripts/aws_lambda_render_acceptance.sh --run

Default mode verifies the configured AWS account, Remotion function, and staging
bucket without launching a render. --run performs one fresh synthetic ranked render
through Katcha's normal HTTP renderer with the Lambda acceptance overlay.
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
    echo "ERROR: verified render-staging bucket metadata is missing." >&2
    exit 2
}

for command in aws docker python3 curl date seq; do
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
    echo "  caller:   ${CALLER_ARN}" >&2
    exit 3
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

echo "Lambda acceptance preflight verified:"
echo "  account:  ${ACTUAL_ACCOUNT}"
echo "  caller:   ${CALLER_ARN}"
echo "  profile:  ${PROFILE}"
echo "  region:   ${REGION}"
echo "  function: ${FUNCTION_NAME}"
echo "  site:     ${SERVE_URL}"
echo "  staging:  s3://${STAGING_BUCKET}/${STAGING_PREFIX}/"

if [[ "${MODE}" == "inspect" ]]; then
    echo "INSPECT ONLY: no render was launched."
    exit 0
fi

cd "${REPO_ROOT}"

COMPOSE=(
    docker compose
    -f docker-compose.yml
    -f docker-compose.aws-render.yml
    -f docker-compose.aws-roles-anywhere.yml
    -f docker-compose.aws-lambda-acceptance.yml
)

renderer_was_running=false
minio_was_running=false
[[ -n "$(docker compose ps --status running -q renderer 2>/dev/null)" ]] && renderer_was_running=true
[[ -n "$(docker compose ps --status running -q minio 2>/dev/null)" ]] && minio_was_running=true

restore_local_state() {
    set +e
    if [[ "${renderer_was_running}" == "true" ]]; then
        docker compose up -d --no-deps --force-recreate renderer >/dev/null 2>&1
    else
        "${COMPOSE[@]}" stop renderer >/dev/null 2>&1
    fi
    if [[ "${minio_was_running}" != "true" ]]; then
        docker compose stop minio >/dev/null 2>&1
    fi
}
trap restore_local_state EXIT

"${COMPOSE[@]}" up -d --build minio minio-init renderer

for attempt in $(seq 1 90); do
    if health="$(curl -fsS http://127.0.0.1:8787/health 2>/dev/null)"; then
        if HEALTH="${health}" EXPECTED_ACCOUNT="${EXPECTED_ACCOUNT}" EXPECTED_REGION="${REGION}" python3 - <<'PY'
import json
import os

health = json.loads(os.environ["HEALTH"])
assert health.get("status") == "ok"
assert health.get("render_backend") == "lambda"
assert health.get("aws_account_id") == os.environ["EXPECTED_ACCOUNT"]
assert health.get("lambda_region") == os.environ["EXPECTED_REGION"]
assert health.get("cloud_staging_enabled") is True
PY
        then
            break
        fi
    fi
    if [[ "${attempt}" -eq 90 ]]; then
        "${COMPOSE[@]}" logs --no-color renderer
        exit 5
    fi
    sleep 2
done

RUN_ID="lambda-acceptance-$(date -u +%Y%m%dT%H%M%SZ)-$$"

KATCHA_S3_ENDPOINT_URL=http://127.0.0.1:9000 KATCHA_S3_ACCESS_KEY=katcha KATCHA_S3_SECRET_KEY=katcha-local-secret KATCHA_S3_BUCKET=katcha-media KATCHA_S3_REGION=us-east-1 KATCHA_S3_FORCE_PATH_STYLE=true KATCHA_RENDERER_URL=http://127.0.0.1:8787 KATCHA_RENDER_SMOKE_RUN_ID="${RUN_ID}" KATCHA_RENDER_SMOKE_EXPECTED_RENDERER=remotion-lambda KATCHA_RENDER_SMOKE_REQUIRE_FRESH=true python3 scripts/ranked_renderer_smoke.py

echo
echo "PASS: controlled Remotion Lambda acceptance completed."
echo "  run_id: ${RUN_ID}"
echo "KATCHA_RENDER_BACKEND was not changed in ${ENV_FILE}."
