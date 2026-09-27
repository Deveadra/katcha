#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
SCRIPT="${REPO_ROOT}/scripts/aws_remotion_deploy.sh"
TMP="$(mktemp -d)"
trap 'rm -rf "${TMP}"' EXIT

BIN="${TMP}/bin"
STATE="${TMP}/state"
ENV_FILE="${TMP}/katcha.env"
LOG="${TMP}/calls.log"
mkdir -p "${BIN}" "${STATE}"

cat >"${ENV_FILE}" <<'EOF'
KATCHA_RENDER_BACKEND=local
KATCHA_AWS_EXPECTED_ACCOUNT_ID=123456789012
KATCHA_REMOTION_LAMBDA_REGION=us-east-1
KATCHA_AWS_PROFILE=katcha-automation
EOF

cat >"${BIN}/node" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
printf 'node ' >>"${DEPLOY_TEST_LOG}"
printf '%q ' "$@" >>"${DEPLOY_TEST_LOG}"
printf '\n' >>"${DEPLOY_TEST_LOG}"

case " $* " in
    *"lambda-deployment-state.mjs function-name "*)
        printf 'remotion-render-4-0-529-mem%smb-disk%smb-%ssec\n' "$3" "$4" "$5"
        ;;
    *"lambda-deployment-state.mjs site "*)
        if [[ ! -f "${DEPLOY_TEST_STATE}/site" ]]; then
            exit 4
        fi
        cat <<'JSON'
{"id":"katcha-production","bucketName":"remotionlambda-test","serveUrl":"https://remotionlambda-test.s3.us-east-1.amazonaws.com/sites/katcha-production/index.html","version":"4.0.529","lastModified":1,"sizeInBytes":123}
JSON
        ;;
    *)
        echo "unexpected node invocation: $*" >&2
        exit 90
        ;;
esac
EOF

cat >"${BIN}/aws" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
printf 'aws ' >>"${DEPLOY_TEST_LOG}"
printf '%q ' "$@" >>"${DEPLOY_TEST_LOG}"
printf '\n' >>"${DEPLOY_TEST_LOG}"

service="${1:-}"
operation="${2:-}"

if [[ "${service} ${operation}" == "sts get-caller-identity" ]]; then
    query=""
    for ((i = 1; i <= $#; i++)); do
        if [[ "${!i}" == "--query" ]]; then
            j=$((i + 1))
            query="${!j}"
        fi
    done
    account="${DEPLOY_TEST_ACCOUNT:-123456789012}"
    case "${query}" in
        Account)
            printf '%s\n' "${account}"
            ;;
        Arn)
            printf 'arn:aws:sts::%s:assumed-role/KatchaChronosAutomation/test\n' "${account}"
            ;;
        *)
            printf '{"Account":"%s","Arn":"arn:aws:sts::%s:assumed-role/KatchaChronosAutomation/test"}\n'                 "${account}" "${account}"
            ;;
    esac
    exit 0
fi

if [[ "${service} ${operation}" == "lambda get-function" ]]; then
    if [[ ! -f "${DEPLOY_TEST_STATE}/function" ]]; then
        exit 254
    fi
    query=""
    for ((i = 1; i <= $#; i++)); do
        if [[ "${!i}" == "--query" ]]; then
            j=$((i + 1))
            query="${!j}"
        fi
    done
    function_name=""
    for ((i = 1; i <= $#; i++)); do
        if [[ "${!i}" == "--function-name" ]]; then
            j=$((i + 1))
            function_name="${!j}"
        fi
    done
    if [[ "${query}" == "Configuration.FunctionName" ]]; then
        printf '%s\n' "${function_name}"
    else
        printf '{"Configuration":{"FunctionName":"%s"}}\n' "${function_name}"
    fi
    exit 0
fi

echo "unexpected aws invocation: $*" >&2
exit 90
EOF

cat >"${BIN}/npm" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
printf 'npm ' >>"${DEPLOY_TEST_LOG}"
printf '%q ' "$@" >>"${DEPLOY_TEST_LOG}"
printf '\n' >>"${DEPLOY_TEST_LOG}"

if [[ "$*" == "ls @remotion/cli --depth=0 --json" ]]; then
    if [[ "${REMOTION_TEST_MISSING:-false}" == "true" ]]; then
        printf '%s\n' '{"dependencies":{}}'
        exit 1
    fi
    printf '{"dependencies":{"@remotion/cli":{"version":"%s"}}}\n' "${REMOTION_TEST_VERSION:-4.0.529}"
    exit 0
fi

case "$*" in
    "run test:config"|"run lambda:policy:validate")
        exit 0
        ;;
esac

echo "unexpected npm invocation: $*" >&2
exit 90
EOF

cat >"${BIN}/npx" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
printf 'npx ' >>"${DEPLOY_TEST_LOG}"
printf '%q ' "$@" >>"${DEPLOY_TEST_LOG}"
printf '\n' >>"${DEPLOY_TEST_LOG}"

case " $* " in
    *" remotion lambda quotas "*)
        exit 0
        ;;
    *" remotion lambda functions deploy "*)
        touch "${DEPLOY_TEST_STATE}/function"
        exit 0
        ;;
    *" remotion lambda sites create "*)
        touch "${DEPLOY_TEST_STATE}/site"
        exit 0
        ;;
    *" remotion lambda compositions "*)
        [[ -f "${DEPLOY_TEST_STATE}/function" ]]
        [[ -f "${DEPLOY_TEST_STATE}/site" ]]
        exit 0
        ;;
    *)
        echo "unexpected npx invocation: $*" >&2
        exit 90
        ;;
esac
EOF

chmod +x "${BIN}/node" "${BIN}/aws" "${BIN}/npm" "${BIN}/npx"

export PATH="${BIN}:${PATH}"
export DEPLOY_TEST_LOG="${LOG}"
export DEPLOY_TEST_STATE="${STATE}"
export KATCHA_ENV_FILE="${ENV_FILE}"
export KATCHA_AWS_ROLES_ANYWHERE_DIR="${TMP}/roles-anywhere"
unset KATCHA_AWS_EXPECTED_ACCOUNT_ID
unset KATCHA_REMOTION_LAMBDA_REGION
unset KATCHA_AWS_PROFILE

: >"${LOG}"
export REMOTION_TEST_MISSING=true
set +e
missing_output="$(bash "${SCRIPT}" 2>&1)"
missing_rc=$?
set -e
unset REMOTION_TEST_MISSING

if [[ "${missing_rc}" -eq 0 ]]; then
    echo "FAIL: missing Remotion dependencies unexpectedly succeeded" >&2
    exit 1
fi
if grep -q '^aws ' "${LOG}" || grep -q '^node ' "${LOG}"; then
    echo "FAIL: missing Remotion dependencies reached AWS/deployment state checks before failing" >&2
    cat "${LOG}" >&2
    exit 1
fi
if ! grep -q 'pinned Remotion CLI is not installed' <<<"${missing_output}"; then
    echo "FAIL: missing dependency error was not actionable" >&2
    exit 1
fi
if ! grep -q 'npm install --ignore-scripts' <<<"${missing_output}"; then
    echo "FAIL: missing dependency error did not include the repair command" >&2
    exit 1
fi

: >"${LOG}"
export REMOTION_TEST_VERSION=4.0.528
set +e
mismatch_output="$(bash "${SCRIPT}" 2>&1)"
mismatch_rc=$?
set -e
unset REMOTION_TEST_VERSION

if [[ "${mismatch_rc}" -eq 0 ]]; then
    echo "FAIL: mismatched Remotion version unexpectedly succeeded" >&2
    exit 1
fi
if grep -q '^aws ' "${LOG}" || grep -q '^node ' "${LOG}"; then
    echo "FAIL: mismatched Remotion version reached AWS/deployment state checks before failing" >&2
    cat "${LOG}" >&2
    exit 1
fi
if ! grep -q 'does not match the pinned renderer dependency' <<<"${mismatch_output}"; then
    echo "FAIL: version mismatch was not reported clearly" >&2
    exit 1
fi

: >"${LOG}"
export KATCHA_REMOTION_LAMBDA_MEMORY_MB=0
set +e
invalid_memory_output="$(bash "${SCRIPT}" 2>&1)"
invalid_memory_rc=$?
set -e
unset KATCHA_REMOTION_LAMBDA_MEMORY_MB

if [[ "${invalid_memory_rc}" -eq 0 ]]; then
    echo "FAIL: invalid Lambda memory unexpectedly succeeded" >&2
    exit 1
fi
if ! grep -q 'KATCHA_REMOTION_LAMBDA_MEMORY_MB must be an integer from 128 through 10240' <<<"${invalid_memory_output}"; then
    echo "FAIL: invalid Lambda memory was not reported clearly" >&2
    exit 1
fi

: >"${LOG}"
export KATCHA_REMOTION_LAMBDA_MEMORY_MB=2048
override_output="$(bash "${SCRIPT}")"
unset KATCHA_REMOTION_LAMBDA_MEMORY_MB

if ! grep -q 'memory:        2048 MB' <<<"${override_output}"; then
    echo "FAIL: Lambda memory override was not honored" >&2
    exit 1
fi
if ! grep -q 'remotion-render-4-0-529-mem2048mb-disk4096mb-900sec' <<<"${override_output}"; then
    echo "FAIL: Lambda memory override did not change deterministic function name" >&2
    exit 1
fi

: >"${LOG}"
inspect_output="$(bash "${SCRIPT}")"

if grep -q 'remotion lambda functions deploy' "${LOG}"; then
    echo "FAIL: inspect mode deployed a Lambda function" >&2
    exit 1
fi
if grep -q 'remotion lambda sites create' "${LOG}"; then
    echo "FAIL: inspect mode deployed a Remotion site" >&2
    exit 1
fi
if ! grep -q 'INSPECT ONLY: no Lambda function or Remotion site was deployed or modified.' <<<"${inspect_output}"; then
    echo "FAIL: inspect mode did not report no-write behavior" >&2
    exit 1
fi
if [[ -e "${STATE}/function" || -e "${STATE}/site" ]]; then
    echo "FAIL: inspect mode changed deployment state" >&2
    exit 1
fi

: >"${LOG}"
apply_output="$(bash "${SCRIPT}" --apply)"

function_deploy_count="$(grep -c 'remotion lambda functions deploy' "${LOG}" || true)"
site_deploy_count="$(grep -c 'remotion lambda sites create' "${LOG}" || true)"
composition_count="$(grep -c 'remotion lambda compositions' "${LOG}" || true)"

if [[ "${function_deploy_count}" -ne 1 ]]; then
    echo "FAIL: apply expected one function deployment, got ${function_deploy_count}" >&2
    exit 1
fi
if [[ "${site_deploy_count}" -ne 1 ]]; then
    echo "FAIL: apply expected one site deployment, got ${site_deploy_count}" >&2
    exit 1
fi
if [[ "${composition_count}" -ne 1 ]]; then
    echo "FAIL: apply expected one composition verification, got ${composition_count}" >&2
    exit 1
fi

grep -q '^KATCHA_REMOTION_LAMBDA_MEMORY_MB=3008$' "${ENV_FILE}"
grep -q '^KATCHA_REMOTION_LAMBDA_FUNCTION_NAME=remotion-render-4-0-529-mem3008mb-disk4096mb-900sec$' "${ENV_FILE}"
grep -q '^KATCHA_REMOTION_LAMBDA_SERVE_URL=https://remotionlambda-test.s3.us-east-1.amazonaws.com/sites/katcha-production/index.html$' "${ENV_FILE}"
grep -q '^KATCHA_RENDER_BACKEND=local$' "${ENV_FILE}"

if ! grep -q 'backend:    local (unchanged)' <<<"${apply_output}"; then
    echo "FAIL: apply did not prove the backend remained local" >&2
    exit 1
fi

backup_count="$(find "${TMP}/roles-anywhere/env-backups" -type f -name 'katcha.env.*.bak' | wc -l)"
if [[ "${backup_count}" -lt 1 ]]; then
    echo "FAIL: apply did not back up the existing env outside the repository" >&2
    exit 1
fi

rm -f "${STATE}/function" "${STATE}/site"
: >"${LOG}"
export DEPLOY_TEST_ACCOUNT=999999999999
set +e
wrong_output="$(bash "${SCRIPT}" --apply 2>&1)"
wrong_rc=$?
set -e
unset DEPLOY_TEST_ACCOUNT

if [[ "${wrong_rc}" -eq 0 ]]; then
    echo "FAIL: wrong-account apply unexpectedly succeeded" >&2
    exit 1
fi
if grep -q 'remotion lambda functions deploy' "${LOG}" || grep -q 'remotion lambda sites create' "${LOG}"; then
    echo "FAIL: wrong-account mode performed a deployment write" >&2
    exit 1
fi
if ! grep -q 'AWS account mismatch' <<<"${wrong_output}"; then
    echo "FAIL: wrong-account mode did not report the mismatch" >&2
    exit 1
fi

echo "PASS: Remotion deployment is inspect-first, account-guarded, verified, and backend-safe."
