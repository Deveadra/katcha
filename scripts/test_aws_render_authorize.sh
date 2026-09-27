#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
SCRIPT="${REPO_ROOT}/scripts/aws_render_authorize.sh"
TMP="$(mktemp -d)"
trap 'rm -rf "${TMP}"' EXIT

BIN="${TMP}/bin"
LOG="${TMP}/aws.log"
CONTROL_POLICY="${TMP}/control-policy.json"
EXECUTION_POLICY="${TMP}/execution-policy.json"
TRUST_POLICY="${TMP}/trust-policy.json"
EXECUTION_ROLE_STATE="${TMP}/execution-role"
mkdir -p "${BIN}"
: >"${LOG}"

cat >"${BIN}/npm" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail

if [[ "$*" == "ls @remotion/cli --depth=0 --json" ]]; then
    if [[ "${REMOTION_TEST_MISSING:-false}" == "true" ]]; then
        printf '%s\n' '{"dependencies":{}}'
        exit 1
    fi
    printf '{"dependencies":{"@remotion/cli":{"version":"%s"}}}\n' "${REMOTION_TEST_VERSION:-4.0.529}"
    exit 0
fi

if [[ "$*" == "run --silent lambda:policy:user" ]]; then
    cat <<'JSON'
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": ["iam:PassRole", "lambda:GetFunction", "lambda:InvokeFunction"],
      "Resource": "*"
    }
  ]
}
JSON
    exit 0
fi

if [[ "$*" == "run --silent lambda:policy:role" ]]; then
    cat <<'JSON'
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": ["s3:GetObject", "s3:PutObject", "lambda:InvokeFunction"],
      "Resource": "*"
    }
  ]
}
JSON
    exit 0
fi

echo "unexpected npm invocation: $*" >&2
exit 90
EOF

cat >"${BIN}/aws" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail

printf '%q ' "$@" >>"${AWS_TEST_LOG}"
printf '\n' >>"${AWS_TEST_LOG}"

service="${1:-}"
operation="${2:-}"

arg_value() {
    local wanted="$1"
    shift
    while (( $# > 0 )); do
        if [[ "$1" == "${wanted}" ]]; then
            [[ $# -ge 2 ]] || return 1
            printf '%s\n' "$2"
            return 0
        fi
        shift
    done
    return 1
}

if [[ "${service} ${operation}" == "sts get-caller-identity" ]]; then
    query="$(arg_value --query "$@" || true)"
    account="${AWS_TEST_ACCOUNT:-123456789012}"
    case "${query}" in
        Account)
            printf '%s\n' "${account}"
            ;;
        Arn)
            printf 'arn:aws:iam::%s:user/bootstrap\n' "${account}"
            ;;
        *)
            printf '{"Account":"%s","Arn":"arn:aws:iam::%s:user/bootstrap"}\n' "${account}" "${account}"
            ;;
    esac
    exit 0
fi

if [[ "${service} ${operation}" == "iam get-role" ]]; then
    role="$(arg_value --role-name "$@")"
    query="$(arg_value --query "$@" || true)"
    account="${AWS_TEST_ACCOUNT:-123456789012}"

    if [[ "${role}" == "remotion-lambda-role" && ! -f "${AWS_TEST_EXECUTION_ROLE_STATE}" ]]; then
        exit 254
    fi

    if [[ "${query}" == "Role.Arn" ]]; then
        printf 'arn:aws:iam::%s:role/%s\n' "${account}" "${role}"
        exit 0
    fi
    if [[ "${query}" == "Role.AssumeRolePolicyDocument" ]]; then
        cat "${AWS_TEST_TRUST_POLICY}"
        exit 0
    fi

    printf '{"Role":{"Arn":"arn:aws:iam::%s:role/%s"}}\n' "${account}" "${role}"
    exit 0
fi

if [[ "${service} ${operation}" == "iam create-role" ]]; then
    role="$(arg_value --role-name "$@")"
    [[ "${role}" == "remotion-lambda-role" ]] || exit 90
    policy_document="$(arg_value --assume-role-policy-document "$@")"
    cp "${policy_document#file://}" "${AWS_TEST_TRUST_POLICY}"
    touch "${AWS_TEST_EXECUTION_ROLE_STATE}"
    printf '{"Role":{"Arn":"arn:aws:iam::123456789012:role/remotion-lambda-role"}}\n'
    exit 0
fi

if [[ "${service} ${operation}" == "iam update-assume-role-policy" ]]; then
    role="$(arg_value --role-name "$@")"
    [[ "${role}" == "remotion-lambda-role" ]] || exit 90
    policy_document="$(arg_value --policy-document "$@")"
    cp "${policy_document#file://}" "${AWS_TEST_TRUST_POLICY}"
    exit 0
fi

if [[ "${service} ${operation}" == "iam put-role-policy" ]]; then
    role="$(arg_value --role-name "$@")"
    policy_document="$(arg_value --policy-document "$@")"
    case "${role}" in
        KatchaChronosAutomation)
            cp "${policy_document#file://}" "${AWS_TEST_CONTROL_POLICY}"
            ;;
        remotion-lambda-role)
            cp "${policy_document#file://}" "${AWS_TEST_EXECUTION_POLICY}"
            ;;
        *)
            exit 90
            ;;
    esac
    exit 0
fi

if [[ "${service} ${operation}" == "iam get-role-policy" ]]; then
    role="$(arg_value --role-name "$@")"
    case "${role}" in
        KatchaChronosAutomation)
            cat "${AWS_TEST_CONTROL_POLICY}"
            ;;
        remotion-lambda-role)
            cat "${AWS_TEST_EXECUTION_POLICY}"
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

chmod +x "${BIN}/aws" "${BIN}/npm"

export PATH="${BIN}:${PATH}"
export AWS_TEST_LOG="${LOG}"
export AWS_TEST_CONTROL_POLICY="${CONTROL_POLICY}"
export AWS_TEST_EXECUTION_POLICY="${EXECUTION_POLICY}"
export AWS_TEST_TRUST_POLICY="${TRUST_POLICY}"
export AWS_TEST_EXECUTION_ROLE_STATE="${EXECUTION_ROLE_STATE}"
export KATCHA_AWS_EXPECTED_ACCOUNT_ID=123456789012
export KATCHA_AWS_BOOTSTRAP_PROFILE=katcha
export KATCHA_REMOTION_LAMBDA_REGION=us-east-1
export KATCHA_IAM_PROPAGATION_WAIT_SECONDS=0

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
if [[ -s "${LOG}" ]]; then
    echo "FAIL: missing Remotion dependencies reached AWS before failing" >&2
    exit 1
fi
grep -q 'pinned Remotion CLI is not installed' <<<"${missing_output}"

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
if [[ -s "${LOG}" ]]; then
    echo "FAIL: version mismatch reached AWS before failing" >&2
    exit 1
fi
grep -q 'expected: 4.0.529' <<<"${mismatch_output}"
grep -q 'actual:   4.0.528' <<<"${mismatch_output}"

: >"${LOG}"
inspect_output="$(bash "${SCRIPT}")"

if grep -Eq 'iam (create-role|update-assume-role-policy|put-role-policy)' "${LOG}"; then
    echo "FAIL: inspect mode performed an IAM write" >&2
    cat "${LOG}" >&2
    exit 1
fi
grep -q 'INSPECT ONLY: no IAM writes were performed.' <<<"${inspect_output}"
grep -q 'state:         missing' <<<"${inspect_output}"
grep -q 'policy name:   remotion-lambda-policy' <<<"${inspect_output}"
grep -q 'trust:         lambda.amazonaws.com -> sts:AssumeRole' <<<"${inspect_output}"

: >"${LOG}"
bash "${SCRIPT}" --apply >"${TMP}/apply.out"

[[ -f "${EXECUTION_ROLE_STATE}" ]]
[[ -s "${CONTROL_POLICY}" ]]
[[ -s "${EXECUTION_POLICY}" ]]
[[ -s "${TRUST_POLICY}" ]]
grep -q 'iam create-role' "${LOG}"
[[ "$(grep -c 'iam put-role-policy' "${LOG}")" -eq 2 ]]
grep -q 'lambda.amazonaws.com' "${TRUST_POLICY}"
grep -q 'PASS: Remotion control-plane authorization and Lambda execution role are verified.' "${TMP}/apply.out"

: >"${LOG}"
bash "${SCRIPT}" --apply >"${TMP}/reapply.out"
if grep -q 'iam create-role' "${LOG}"; then
    echo "FAIL: idempotent re-apply attempted to recreate execution role" >&2
    exit 1
fi
grep -q 'iam update-assume-role-policy' "${LOG}"
[[ "$(grep -c 'iam put-role-policy' "${LOG}")" -eq 2 ]]

: >"${LOG}"
export AWS_TEST_ACCOUNT=999999999999
set +e
wrong_output="$(bash "${SCRIPT}" 2>&1)"
wrong_rc=$?
set -e
unset AWS_TEST_ACCOUNT

if [[ "${wrong_rc}" -eq 0 ]]; then
    echo "FAIL: wrong-account mode unexpectedly succeeded" >&2
    exit 1
fi
if grep -Eq 'iam (create-role|update-assume-role-policy|put-role-policy)' "${LOG}"; then
    echo "FAIL: wrong-account mode performed an IAM write" >&2
    exit 1
fi
grep -q 'AWS account mismatch' <<<"${wrong_output}"

echo "PASS: render authorization provisions the exact Remotion execution role, is idempotent, review-first, and account-guarded."
