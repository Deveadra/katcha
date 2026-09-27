#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd)"
RENDERER_DIR="${REPO_ROOT}/renderer"
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
PROFILE="${KATCHA_AWS_PROFILE:-$(read_env_key KATCHA_AWS_PROFILE)}"
PROFILE="${PROFILE:-${AWS_PROFILE:-katcha-automation}}"
REGION="${REGION:-us-east-1}"

if [[ ! "${EXPECTED_ACCOUNT}" =~ ^[0-9]{12}$ ]]; then
    echo "ERROR: KATCHA_AWS_EXPECTED_ACCOUNT_ID must be the intended 12-digit AWS account ID." >&2
    exit 2
fi

if [[ -z "${REGION}" ]]; then
    echo "ERROR: KATCHA_REMOTION_LAMBDA_REGION must be set explicitly." >&2
    exit 2
fi

for command in aws node npm npx python3; do
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
REMOTION_TREE="$(
    cd "${RENDERER_DIR}"
    npm ls @remotion/cli --depth=0 --json 2>/dev/null
)"
REMOTION_TREE_RC=$?
set -e

if [[ "${REMOTION_TREE_RC}" -ne 0 || -z "${REMOTION_TREE}" ]]; then
    echo "ERROR: the pinned Remotion CLI is not installed in renderer/node_modules." >&2
    echo "Install renderer dependencies before AWS/Remotion preflight:" >&2
    echo "  cd ${RENDERER_DIR}" >&2
    echo "  npm install --ignore-scripts" >&2
    exit 2
fi

set +e
REMOTION_VERSION="$(
    printf '%s' "${REMOTION_TREE}" | python3 -c '
import json
import sys
tree = json.load(sys.stdin)
print(tree["dependencies"]["@remotion/cli"]["version"])
'
)"
REMOTION_VERSION_RC=$?
set -e

if [[ "${REMOTION_VERSION_RC}" -ne 0 || -z "${REMOTION_VERSION}" ]]; then
    echo "ERROR: installed @remotion/cli metadata could not be verified." >&2
    echo "Reinstall renderer dependencies before continuing:" >&2
    echo "  cd ${RENDERER_DIR}" >&2
    echo "  npm install --ignore-scripts" >&2
    exit 2
fi

if [[ "${REMOTION_VERSION}" != "${EXPECTED_REMOTION_VERSION}" ]]; then
    echo "ERROR: installed Remotion CLI version does not match the pinned renderer dependency." >&2
    echo "  expected: ${EXPECTED_REMOTION_VERSION}" >&2
    echo "  actual:   ${REMOTION_VERSION}" >&2
    echo "Reinstall renderer dependencies before continuing:" >&2
    echo "  cd ${RENDERER_DIR}" >&2
    echo "  npm install --ignore-scripts" >&2
    exit 2
fi

echo "Remotion CLI verified: ${REMOTION_VERSION}"

ACTUAL_ACCOUNT="$(aws sts get-caller-identity --profile "${PROFILE}" --region "${REGION}" --query Account --output text)"
CALLER_ARN="$(aws sts get-caller-identity --profile "${PROFILE}" --region "${REGION}" --query Arn --output text)"

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
echo "  profile: ${PROFILE}"
echo "  region:  ${REGION}"

pushd "${RENDERER_DIR}" >/dev/null
AWS_PROFILE="${PROFILE}" REMOTION_AWS_PROFILE="${PROFILE}" npm run test:config
AWS_PROFILE="${PROFILE}" REMOTION_AWS_PROFILE="${PROFILE}" npm run lambda:policy:validate
AWS_PROFILE="${PROFILE}" REMOTION_AWS_PROFILE="${PROFILE}" npx remotion lambda quotas --region="${REGION}"
popd >/dev/null

echo "PASS: AWS/Remotion preflight completed with no resource-creation command."
