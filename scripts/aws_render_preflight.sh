#!/usr/bin/env bash
set -euo pipefail

EXPECTED_ACCOUNT="${KATCHA_AWS_EXPECTED_ACCOUNT_ID:-}"
REGION="${KATCHA_REMOTION_LAMBDA_REGION:-}"

if [[ ! "${EXPECTED_ACCOUNT}" =~ ^[0-9]{12}$ ]]; then
    echo "ERROR: KATCHA_AWS_EXPECTED_ACCOUNT_ID must be the intended 12-digit AWS account ID." >&2
    exit 2
fi

if [[ -z "${REGION}" ]]; then
    echo "ERROR: KATCHA_REMOTION_LAMBDA_REGION must be set explicitly." >&2
    exit 2
fi

for command in aws node npm npx; do
    if ! command -v "${command}" >/dev/null 2>&1; then
        echo "ERROR: required command is not installed: ${command}" >&2
        exit 2
    fi
done

ACTUAL_ACCOUNT="$(aws sts get-caller-identity --query Account --output text)"
CALLER_ARN="$(aws sts get-caller-identity --query Arn --output text)"

if [[ "${ACTUAL_ACCOUNT}" != "${EXPECTED_ACCOUNT}" ]]; then
    echo "ERROR: AWS account mismatch." >&2
    echo "  expected: ${EXPECTED_ACCOUNT}" >&2
    echo "  actual:   ${ACTUAL_ACCOUNT}" >&2
    echo "  caller:   ${CALLER_ARN}" >&2
    exit 3
fi

echo "AWS identity verified:"
echo "  account: ${ACTUAL_ACCOUNT}"
echo "  caller:  ${CALLER_ARN}"
echo "  region:  ${REGION}"

pushd renderer >/dev/null
npm run test:config
npm run lambda:policy:validate
npx remotion lambda quotas --region="${REGION}"
popd >/dev/null

echo "PASS: AWS/Remotion preflight completed with no resource-creation command."
