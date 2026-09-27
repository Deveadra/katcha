#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
SCRIPT="${REPO_ROOT}/scripts/aws_lambda_backend_enable.sh"
TMP="$(mktemp -d)"
trap 'rm -rf "${TMP}"' EXIT

mkdir -p "${TMP}/bin" "${TMP}/home/.aws/katcha-roles-anywhere/acceptance"
ENV_FILE="${TMP}/katcha.env"
RECEIPT="${TMP}/home/.aws/katcha-roles-anywhere/acceptance/latest.json"
LOG="${TMP}/calls.log"
COMMIT="$(git -C "${REPO_ROOT}" rev-parse HEAD)"

cat >"${ENV_FILE}" <<'EOF'
KATCHA_AWS_EXPECTED_ACCOUNT_ID=123456789012
KATCHA_REMOTION_LAMBDA_REGION=us-east-1
KATCHA_AWS_PROFILE=katcha-automation
KATCHA_REMOTION_LAMBDA_FUNCTION_NAME=remotion-render-4-0-529-mem4096mb-disk4096mb-900sec
KATCHA_REMOTION_LAMBDA_SERVE_URL=https://example.invalid/sites/katcha-production
KATCHA_REMOTION_STAGING_BUCKET=katcha-render-staging-123456789012-us-east-1
KATCHA_REMOTION_STAGING_PREFIX=katcha-render-staging
KATCHA_RENDER_BACKEND=local
EOF

python3 - "${RECEIPT}" "${COMMIT}" <<'PY'
from datetime import datetime, timezone
import json
from pathlib import Path
import sys

path = Path(sys.argv[1])
commit = sys.argv[2]
receipt = {
    "version": 1,
    "accepted_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    "account_id": "123456789012",
    "region": "us-east-1",
    "aws_profile": "katcha-automation",
    "function_name": "remotion-render-4-0-529-mem4096mb-disk4096mb-900sec",
    "serve_url": "https://example.invalid/sites/katcha-production",
    "staging_bucket": "katcha-render-staging-123456789012-us-east-1",
    "staging_prefix": "katcha-render-staging",
    "git_commit": commit,
    "run_id": "lambda-acceptance-test",
    "output_key": "ci/ranked-render/lambda-acceptance-test/output.mp4",
    "renderer": "remotion-lambda",
    "verified": True,
    "reused": False,
    "render_id": "render-test-123",
    "lambdas_invoked": 3,
    "object_size_bytes": 12345,
}
path.write_text(json.dumps(receipt), encoding="utf-8")
PY
chmod 600 "${RECEIPT}"

cat >"${TMP}/bin/aws" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
echo "aws $*" >>"${MOCK_LOG}"
if [[ "$*" == *"sts get-caller-identity"* && "$*" == *"--query Account"* ]]; then
    echo "123456789012"
elif [[ "$*" == *"sts get-caller-identity"* && "$*" == *"--query Arn"* ]]; then
    echo "arn:aws:sts::123456789012:assumed-role/KatchaChronosAutomation/test"
elif [[ "$*" == *"lambda get-function"* ]]; then
    echo "remotion-render-4-0-529-mem4096mb-disk4096mb-900sec"
elif [[ "$*" == *"s3api get-bucket-location"* ]]; then
    echo "None"
else
    echo "unexpected aws invocation: $*" >&2
    exit 91
fi
EOF
chmod +x "${TMP}/bin/aws"

export PATH="${TMP}/bin:${PATH}"
export HOME="${TMP}/home"
export MOCK_LOG="${LOG}"
export KATCHA_ENV_FILE="${ENV_FILE}"
export KATCHA_LAMBDA_ACCEPTANCE_RECEIPT="${RECEIPT}"

echo "Testing inspect mode..."
: >"${LOG}"
bash "${SCRIPT}" >"${TMP}/inspect.out"
grep -q "INSPECT ONLY" "${TMP}/inspect.out"
grep -q '^KATCHA_RENDER_BACKEND=local$' "${ENV_FILE}"

echo "Testing apply mode..."
bash "${SCRIPT}" --apply >"${TMP}/apply.out"
grep -q "PASS: Katcha Lambda rendering is now enabled" "${TMP}/apply.out"
grep -q '^KATCHA_RENDER_BACKEND=lambda$' "${ENV_FILE}"
test "$(grep -c '^KATCHA_RENDER_BACKEND=' "${ENV_FILE}")" -eq 1

echo "Testing mismatched receipt rejection..."
python3 - "${RECEIPT}" <<'PY'
import json
from pathlib import Path
import sys
p=Path(sys.argv[1]); data=json.loads(p.read_text()); data["account_id"]="999999999999"
p.write_text(json.dumps(data), encoding="utf-8")
PY
if bash "${SCRIPT}" >"${TMP}/mismatch.out" 2>&1; then
    echo "ERROR: mismatched acceptance receipt was accepted" >&2
    exit 1
fi
grep -q "acceptance receipt mismatch for account_id" "${TMP}/mismatch.out"

echo "Testing stale receipt rejection..."
python3 - "${RECEIPT}" "${COMMIT}" <<'PY'
import json
from pathlib import Path
import sys
p=Path(sys.argv[1]); data=json.loads(p.read_text())
data["account_id"]="123456789012"
data["git_commit"]=sys.argv[2]
data["accepted_at_utc"]="2000-01-01T00:00:00Z"
p.write_text(json.dumps(data), encoding="utf-8")
PY
if bash "${SCRIPT}" >"${TMP}/stale.out" 2>&1; then
    echo "ERROR: stale acceptance receipt was accepted" >&2
    exit 1
fi
grep -q "acceptance receipt is stale" "${TMP}/stale.out"

echo "PASS: Lambda backend enablement is receipt-gated and fail-closed."
