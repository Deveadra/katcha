#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SCRIPT="${ROOT}/scripts/aws_render_preflight.sh"
TMP="$(mktemp -d)"
trap 'rm -rf "${TMP}"' EXIT

BIN="${TMP}/bin"
LOG="${TMP}/calls.log"
ENV_FILE="${TMP}/katcha.env"
mkdir -p "${BIN}"
: >"${LOG}"

cat >"${ENV_FILE}" <<'EOF'
KATCHA_AWS_EXPECTED_ACCOUNT_ID=123456789012
KATCHA_REMOTION_LAMBDA_REGION=us-east-1
KATCHA_AWS_PROFILE=katcha-automation
EOF

cat >"${BIN}/aws" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
printf 'aws ' >>"${PREFLIGHT_TEST_LOG}"
printf '%q ' "$@" >>"${PREFLIGHT_TEST_LOG}"
printf '\n' >>"${PREFLIGHT_TEST_LOG}"

if [[ "${1:-} ${2:-}" == "sts get-caller-identity" ]]; then
    query=""
    for ((i = 1; i <= $#; i++)); do
        if [[ "${!i}" == "--query" ]]; then
            j=$((i + 1))
            query="${!j}"
        fi
    done
    account="${PREFLIGHT_TEST_ACCOUNT:-123456789012}"
    case "${query}" in
        Account)
            printf '%s\n' "${account}"
            ;;
        Arn)
            printf 'arn:aws:sts::%s:assumed-role/KatchaChronosAutomation/test\n' "${account}"
            ;;
        *)
            exit 90
            ;;
    esac
    exit 0
fi

echo "unexpected aws invocation: $*" >&2
exit 90
EOF

cat >"${BIN}/npm" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
printf 'npm ' >>"${PREFLIGHT_TEST_LOG}"
printf '%q ' "$@" >>"${PREFLIGHT_TEST_LOG}"
printf '\n' >>"${PREFLIGHT_TEST_LOG}"

if [[ "$*" == "ls @remotion/cli --depth=0 --json" ]]; then
    printf '%s\n' '{"dependencies":{"@remotion/cli":{"version":"4.0.529"}}}'
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
printf 'npx ' >>"${PREFLIGHT_TEST_LOG}"
printf '%q ' "$@" >>"${PREFLIGHT_TEST_LOG}"
printf '\n' >>"${PREFLIGHT_TEST_LOG}"
if [[ "$*" == "remotion lambda quotas --region=us-east-1" ]]; then
    exit 0
fi
echo "unexpected npx invocation: $*" >&2
exit 90
EOF

chmod +x "${BIN}/aws" "${BIN}/npm" "${BIN}/npx"

export PATH="${BIN}:${PATH}"
export PREFLIGHT_TEST_LOG="${LOG}"
export KATCHA_ENV_FILE="${ENV_FILE}"
unset KATCHA_AWS_EXPECTED_ACCOUNT_ID
unset KATCHA_REMOTION_LAMBDA_REGION
unset KATCHA_AWS_PROFILE
unset AWS_PROFILE

output="$(bash "${SCRIPT}")"
grep -q 'PASS: AWS/Remotion preflight completed with no resource-creation command.' <<<"${output}"
grep -q 'profile: katcha-automation' <<<"${output}"
grep -q -- '--profile katcha-automation' "${LOG}"

# Environment overrides must still take precedence over persisted local metadata.
: >"${LOG}"
export KATCHA_AWS_PROFILE=override-profile
output="$(bash "${SCRIPT}")"
grep -q 'profile: override-profile' <<<"${output}"
grep -q -- '--profile override-profile' "${LOG}"
unset KATCHA_AWS_PROFILE

# Wrong account must fail before renderer validation/quota checks.
: >"${LOG}"
export PREFLIGHT_TEST_ACCOUNT=999999999999
set +e
wrong_output="$(bash "${SCRIPT}" 2>&1)"
wrong_rc=$?
set -e
unset PREFLIGHT_TEST_ACCOUNT
if [[ "${wrong_rc}" -eq 0 ]]; then
    echo "FAIL: wrong-account preflight unexpectedly succeeded" >&2
    exit 1
fi
if ! grep -q 'AWS account mismatch' <<<"${wrong_output}"; then
    echo "FAIL: wrong-account preflight did not report mismatch" >&2
    exit 1
fi
if grep -q 'run test:config' "${LOG}" || grep -q 'remotion lambda quotas' "${LOG}"; then
    echo "FAIL: wrong-account preflight continued into renderer/quota checks" >&2
    exit 1
fi

echo "PASS: AWS render preflight resolves durable local config, preserves overrides, and fails closed."
