#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd)"
RENDERER_DIR="${REPO_ROOT}/renderer"

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

for command in aws node npm npx python3 awk; do
    if ! command -v "${command}" >/dev/null 2>&1; then
        echo "ERROR: required command is not installed: ${command}" >&2
        exit 2
    fi
done

if [[ ! -f "${RENDERER_DIR}/package.json" ]]; then
    echo "ERROR: renderer/package.json was not found under ${REPO_ROOT}." >&2
    exit 2
fi

EXPECTED_REMOTION_VERSION="$(
    python3 - "${RENDERER_DIR}/package.json" <<'PY'
import json
import re
from pathlib import Path
import sys

package = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
version = package.get("dependencies", {}).get("@remotion/cli")
if not isinstance(version, str) or not re.fullmatch(r"\d+\.\d+\.\d+", version):
    raise SystemExit(
        "renderer/package.json must pin @remotion/cli to an exact x.y.z version"
    )
print(version)
PY
)"

set +e
REMOTION_VERSION="$(
    cd "${RENDERER_DIR}"
    npx --no-install remotion --version 2>/dev/null
)"
REMOTION_VERSION_RC=$?
set -e

if [[ "${REMOTION_VERSION_RC}" -ne 0 ]]; then
    echo "ERROR: the pinned Remotion CLI is not installed in renderer/node_modules." >&2
    echo "Install renderer dependencies before AWS/Remotion preflight:" >&2
    echo "  cd ${RENDERER_DIR}" >&2
    echo "  npm install --ignore-scripts" >&2
    exit 2
fi

REMOTION_VERSION="$(printf '%s\n' "${REMOTION_VERSION}" | awk 'NF {last=$0} END {print last}')"
if [[ "${REMOTION_VERSION}" != "${EXPECTED_REMOTION_VERSION}" ]]; then
    echo "ERROR: installed Remotion CLI version does not match the pinned renderer dependency." >&2
    echo "  expected: ${EXPECTED_REMOTION_VERSION}" >&2
    echo "  actual:   ${REMOTION_VERSION:-<empty>}" >&2
    echo "Reinstall renderer dependencies before continuing:" >&2
    echo "  cd ${RENDERER_DIR}" >&2
    echo "  npm install --ignore-scripts" >&2
    exit 2
fi

echo "Remotion CLI verified: ${REMOTION_VERSION}"

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

pushd "${RENDERER_DIR}" >/dev/null
npm run test:config
npm run lambda:policy:validate
npx remotion lambda quotas --region="${REGION}"
popd >/dev/null

echo "PASS: AWS/Remotion preflight completed with no resource-creation command."
