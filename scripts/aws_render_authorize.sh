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
  bash scripts/aws_render_authorize.sh
  bash scripts/aws_render_authorize.sh --apply

Default mode is inspect-only and performs no IAM writes.

Configuration may come from environment variables or Katcha's local .env.

Required configuration:
  KATCHA_AWS_EXPECTED_ACCOUNT_ID   Exact 12-digit AWS account ID.

Optional configuration:
  KATCHA_AWS_BOOTSTRAP_PROFILE     Authorized bootstrap/admin profile.
                                   Default: katcha
  KATCHA_ENV_FILE                  Local env file. Default: .env
  KATCHA_REMOTION_LAMBDA_REGION    AWS region. Default: us-east-1
  KATCHA_AWS_AUTOMATION_ROLE       Dedicated durable renderer role.
                                   Default: KatchaChronosAutomation

This helper generates both Remotion policies from the renderer's pinned dependencies.
It attaches the control-plane policy to Katcha's durable automation role and ensures
the exact Lambda execution role "remotion-lambda-role" exists with Lambda trust and
the generated role policy. It does not grant Katcha staging-bucket access; that policy
remains Terraform-generated and separate.
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
BOOTSTRAP_PROFILE="${KATCHA_AWS_BOOTSTRAP_PROFILE:-katcha}"
REGION="${KATCHA_REMOTION_LAMBDA_REGION:-$(read_env_key KATCHA_REMOTION_LAMBDA_REGION)}"
AUTOMATION_ROLE="${KATCHA_AWS_AUTOMATION_ROLE:-KatchaChronosAutomation}"
REGION="${REGION:-us-east-1}"

POLICY_NAME="KatchaRemotionControlPlane"
EXECUTION_ROLE_NAME="remotion-lambda-role"
EXECUTION_POLICY_NAME="remotion-lambda-policy"

if [[ ! "${EXPECTED_ACCOUNT}" =~ ^[0-9]{12}$ ]]; then
    echo "ERROR: KATCHA_AWS_EXPECTED_ACCOUNT_ID must be the intended 12-digit AWS account ID." >&2
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

for command in aws npm python3 sha256sum mktemp; do
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
    echo "Install renderer dependencies before running AWS authorization:" >&2
    echo "  cd ${RENDERER_DIR}" >&2
    echo "  npm install --ignore-scripts" >&2
    echo "Then rerun this helper. No IAM writes were attempted." >&2
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
    echo "No IAM writes were attempted." >&2
    exit 2
fi

if [[ "${REMOTION_VERSION}" != "${EXPECTED_REMOTION_VERSION}" ]]; then
    echo "ERROR: installed Remotion CLI version does not match the pinned renderer dependency." >&2
    echo "  expected: ${EXPECTED_REMOTION_VERSION}" >&2
    echo "  actual:   ${REMOTION_VERSION}" >&2
    echo "Reinstall renderer dependencies before continuing:" >&2
    echo "  cd ${RENDERER_DIR}" >&2
    echo "  npm install --ignore-scripts" >&2
    echo "No IAM writes were attempted." >&2
    exit 2
fi

echo "Remotion CLI verified: ${REMOTION_VERSION}"

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

ROLE_ARN="$(
    aws iam get-role         --profile "${BOOTSTRAP_PROFILE}"         --role-name "${AUTOMATION_ROLE}"         --query Role.Arn         --output text
)"

expected_role_arn="arn:aws:iam::${EXPECTED_ACCOUNT}:role/${AUTOMATION_ROLE}"
if [[ "${ROLE_ARN}" != "${expected_role_arn}" && "${ROLE_ARN}" != arn:aws:iam::"${EXPECTED_ACCOUNT}":role/*/"${AUTOMATION_ROLE}" ]]; then
    echo "ERROR: resolved IAM role ARN does not belong to the expected account/role." >&2
    echo "  expected role: ${AUTOMATION_ROLE}" >&2
    echo "  actual ARN:    ${ROLE_ARN}" >&2
    exit 4
fi

raw_policy="$(mktemp)"
canonical_policy="$(mktemp)"
verified_policy="$(mktemp)"
raw_execution_policy="$(mktemp)"
canonical_execution_policy="$(mktemp)"
verified_execution_policy="$(mktemp)"
trust_policy="$(mktemp)"
verified_trust_policy="$(mktemp)"
trap 'rm -f "${raw_policy}" "${canonical_policy}" "${verified_policy}" "${raw_execution_policy}" "${canonical_execution_policy}" "${verified_execution_policy}" "${trust_policy}" "${verified_trust_policy}"' EXIT

echo "Generating Remotion control-plane policy from pinned renderer dependencies..."
(
    cd "${RENDERER_DIR}"
    npm run --silent lambda:policy:user
) >"${raw_policy}"

python3 - "${raw_policy}" "${canonical_policy}" <<'PY'
import json
from pathlib import Path
import sys

source = Path(sys.argv[1])
destination = Path(sys.argv[2])

try:
    policy = json.loads(source.read_text(encoding="utf-8"))
except json.JSONDecodeError as exc:
    raise SystemExit(f"generated Remotion policy is not valid JSON: {exc}") from exc

if not isinstance(policy, dict):
    raise SystemExit("generated Remotion policy must be a JSON object")
statements = policy.get("Statement")
if not isinstance(statements, list) or not statements:
    raise SystemExit("generated Remotion policy has no Statement array")

allow_statements = 0
actions = set()
resources = set()
for statement in statements:
    if not isinstance(statement, dict):
        raise SystemExit("generated Remotion policy contains a non-object statement")
    if statement.get("Effect") == "Allow":
        allow_statements += 1

    action = statement.get("Action", [])
    if isinstance(action, str):
        action = [action]
    if not isinstance(action, list):
        raise SystemExit("generated Remotion policy contains invalid Action")
    actions.update(str(item) for item in action)

    resource = statement.get("Resource", [])
    if isinstance(resource, str):
        resource = [resource]
    if not isinstance(resource, list):
        raise SystemExit("generated Remotion policy contains invalid Resource")
    resources.update(str(item) for item in resource)

if allow_statements == 0 or not actions:
    raise SystemExit("generated Remotion policy contains no usable Allow actions")

destination.write_text(
    json.dumps(policy, sort_keys=True, separators=(",", ":")) + "\n",
    encoding="utf-8",
)

print(f"  statements: {len(statements)}")
print(f"  allow statements: {allow_statements}")
print(f"  unique actions: {len(actions)}")
print(f"  unique resources: {len(resources)}")
print("  actions:")
for action in sorted(actions):
    print(f"    - {action}")
PY

POLICY_SHA256="$(sha256sum "${canonical_policy}" | awk '{print $1}')"

echo
echo "Generating Remotion Lambda execution policy from pinned renderer dependencies..."
(
    cd "${RENDERER_DIR}"
    npm run --silent lambda:policy:role
) >"${raw_execution_policy}"

python3 - "${raw_execution_policy}" "${canonical_execution_policy}" <<'PY'
import json
from pathlib import Path
import sys

source = Path(sys.argv[1])
destination = Path(sys.argv[2])

try:
    policy = json.loads(source.read_text(encoding="utf-8"))
except json.JSONDecodeError as exc:
    raise SystemExit(f"generated Remotion execution policy is not valid JSON: {exc}") from exc

if not isinstance(policy, dict):
    raise SystemExit("generated Remotion execution policy must be a JSON object")
statements = policy.get("Statement")
if not isinstance(statements, list) or not statements:
    raise SystemExit("generated Remotion execution policy has no Statement array")

actions = set()
for statement in statements:
    if not isinstance(statement, dict):
        raise SystemExit("generated Remotion execution policy contains a non-object statement")
    action = statement.get("Action", [])
    if isinstance(action, str):
        action = [action]
    if not isinstance(action, list):
        raise SystemExit("generated Remotion execution policy contains invalid Action")
    actions.update(str(item) for item in action)

if not actions:
    raise SystemExit("generated Remotion execution policy contains no actions")

destination.write_text(
    json.dumps(policy, sort_keys=True, separators=(",", ":")) + "\n",
    encoding="utf-8",
)
print(f"  execution-policy statements: {len(statements)}")
print(f"  execution-policy unique actions: {len(actions)}")
PY

EXECUTION_POLICY_SHA256="$(sha256sum "${canonical_execution_policy}" | awk '{print $1}')"

cat >"${trust_policy}" <<'JSON'
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Principal": {
        "Service": "lambda.amazonaws.com"
      },
      "Action": "sts:AssumeRole"
    }
  ]
}
JSON

EXECUTION_ROLE_ARN="$(aws iam get-role     --profile "${BOOTSTRAP_PROFILE}"     --role-name "${EXECUTION_ROLE_NAME}"     --query Role.Arn     --output text 2>/dev/null || true)"
if [[ -z "${EXECUTION_ROLE_ARN}" || "${EXECUTION_ROLE_ARN}" == "None" ]]; then
    EXECUTION_ROLE_STATE="missing"
    EXECUTION_ROLE_ARN="arn:aws:iam::${EXPECTED_ACCOUNT}:role/${EXECUTION_ROLE_NAME}"
else
    EXECUTION_ROLE_STATE="present"
fi

echo
echo "AWS identity verified:"
echo "  account:       ${ACTUAL_ACCOUNT}"
echo "  caller:        ${CALLER_ARN}"
echo "  region:        ${REGION}"
echo "  target role:   ${ROLE_ARN}"
echo "  policy name:   ${POLICY_NAME}"
echo "  policy sha256: ${POLICY_SHA256}"
echo
echo "Remotion Lambda execution role:"
echo "  role:          ${EXECUTION_ROLE_ARN}"
echo "  state:         ${EXECUTION_ROLE_STATE}"
echo "  policy name:   ${EXECUTION_POLICY_NAME}"
echo "  policy sha256: ${EXECUTION_POLICY_SHA256}"
echo "  trust:         lambda.amazonaws.com -> sts:AssumeRole"
echo
echo "Staging-bucket permissions are intentionally NOT included here."
echo "They remain a separate least-privilege policy generated by Terraform."

if [[ "${MODE}" == "inspect" ]]; then
    echo
    echo "INSPECT ONLY: no IAM writes were performed."
    echo "Run again with --apply only after reviewing the action list above."
    exit 0
fi

echo
echo "Applying Remotion control-plane policy to the dedicated automation role..."
aws iam put-role-policy     --profile "${BOOTSTRAP_PROFILE}"     --role-name "${AUTOMATION_ROLE}"     --policy-name "${POLICY_NAME}"     --policy-document "file://${canonical_policy}"

aws iam get-role-policy     --profile "${BOOTSTRAP_PROFILE}"     --role-name "${AUTOMATION_ROLE}"     --policy-name "${POLICY_NAME}"     --query PolicyDocument     --output json >"${verified_policy}"

echo
if [[ "${EXECUTION_ROLE_STATE}" == "missing" ]]; then
    echo "Creating Remotion Lambda execution role..."
    aws iam create-role         --profile "${BOOTSTRAP_PROFILE}"         --role-name "${EXECUTION_ROLE_NAME}"         --description "Execution role for Remotion Lambda render workers managed by Katcha"         --assume-role-policy-document "file://${trust_policy}"         --tags Key=Project,Value=Katcha Key=ManagedBy,Value=aws_render_authorize         >/dev/null
else
    echo "Refreshing Remotion Lambda execution-role trust policy..."
    aws iam update-assume-role-policy         --profile "${BOOTSTRAP_PROFILE}"         --role-name "${EXECUTION_ROLE_NAME}"         --policy-document "file://${trust_policy}"
fi

echo "Applying pinned Remotion execution policy..."
aws iam put-role-policy     --profile "${BOOTSTRAP_PROFILE}"     --role-name "${EXECUTION_ROLE_NAME}"     --policy-name "${EXECUTION_POLICY_NAME}"     --policy-document "file://${canonical_execution_policy}"

aws iam get-role-policy     --profile "${BOOTSTRAP_PROFILE}"     --role-name "${EXECUTION_ROLE_NAME}"     --policy-name "${EXECUTION_POLICY_NAME}"     --query PolicyDocument     --output json >"${verified_execution_policy}"

aws iam get-role     --profile "${BOOTSTRAP_PROFILE}"     --role-name "${EXECUTION_ROLE_NAME}"     --query Role.AssumeRolePolicyDocument     --output json >"${verified_trust_policy}"

python3 - "${canonical_policy}" "${verified_policy}" <<'PY'
import json
from pathlib import Path
import sys

expected = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
actual = json.loads(Path(sys.argv[2]).read_text(encoding="utf-8"))

if expected != actual:
    raise SystemExit("ERROR: IAM policy verification mismatch after PutRolePolicy")
PY

python3 - "${canonical_execution_policy}" "${verified_execution_policy}" "${trust_policy}" "${verified_trust_policy}" <<'PY'
import json
from pathlib import Path
import sys

expected_policy = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
actual_policy = json.loads(Path(sys.argv[2]).read_text(encoding="utf-8"))
expected_trust = json.loads(Path(sys.argv[3]).read_text(encoding="utf-8"))
actual_trust = json.loads(Path(sys.argv[4]).read_text(encoding="utf-8"))

if expected_policy != actual_policy:
    raise SystemExit("ERROR: Remotion execution policy verification mismatch")
if expected_trust != actual_trust:
    raise SystemExit("ERROR: Remotion execution-role trust policy verification mismatch")
PY

EXECUTION_ROLE_ARN="$(aws iam get-role     --profile "${BOOTSTRAP_PROFILE}"     --role-name "${EXECUTION_ROLE_NAME}"     --query Role.Arn     --output text)"

EXPECTED_EXECUTION_ROLE_ARN="arn:aws:iam::${EXPECTED_ACCOUNT}:role/${EXECUTION_ROLE_NAME}"
if [[ "${EXECUTION_ROLE_ARN}" != "${EXPECTED_EXECUTION_ROLE_ARN}" ]]; then
    echo "ERROR: Remotion execution role resolved to an unexpected ARN." >&2
    echo "  expected: ${EXPECTED_EXECUTION_ROLE_ARN}" >&2
    echo "  actual:   ${EXECUTION_ROLE_ARN}" >&2
    exit 5
fi

echo "Waiting briefly for IAM role/trust propagation before Lambda deployment..."
sleep 10

echo
echo "PASS: Remotion control-plane authorization and Lambda execution role are verified."
echo "  role:          ${ROLE_ARN}"
echo "  policy name:   ${POLICY_NAME}"
echo "  policy sha256: ${POLICY_SHA256}"
echo "  execution role:${EXECUTION_ROLE_ARN}"
echo "  execution policy sha256: ${EXECUTION_POLICY_SHA256}"
echo
echo "Next gate: validate permissions with the durable katcha-automation profile,"
echo "then deploy the pinned Remotion function/site and provision staging separately."
