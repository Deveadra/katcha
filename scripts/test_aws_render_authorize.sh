#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
SCRIPT="${REPO_ROOT}/scripts/aws_render_authorize.sh"
TMP="$(mktemp -d)"
trap 'rm -rf "${TMP}"' EXIT

BIN="${TMP}/bin"
mkdir -p "${BIN}"
LOG="${TMP}/aws.log"
POLICY_CAPTURE="${TMP}/applied-policy.json"

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
      "Action": ["lambda:GetFunction", "lambda:InvokeFunction"],
      "Resource": "*"
    },
    {
      "Effect": "Allow",
      "Action": "s3:GetObject",
      "Resource": "arn:aws:s3:::remotionlambda-*/*"
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

if [[ "${service} ${operation}" == "sts get-caller-identity" ]]; then
    query=""
    for ((i = 1; i <= $#; i++)); do
        if [[ "${!i}" == "--query" ]]; then
            j=$((i + 1))
            query="${!j}"
        fi
    done
    case "${query}" in
        Account)
            printf '%s\n' "${AWS_TEST_ACCOUNT:-123456789012}"
            ;;
        Arn)
            printf 'arn:aws:iam::%s:user/bootstrap\n' "${AWS_TEST_ACCOUNT:-123456789012}"
            ;;
        *)
            printf '{"Account":"%s","Arn":"arn:aws:iam::%s:user/bootstrap"}\n'                 "${AWS_TEST_ACCOUNT:-123456789012}"                 "${AWS_TEST_ACCOUNT:-123456789012}"
            ;;
    esac
    exit 0
fi

if [[ "${service} ${operation}" == "iam get-role" ]]; then
    role=""
    query=""
    for ((i = 1; i <= $#; i++)); do
        case "${!i}" in
            --role-name)
                j=$((i + 1))
                role="${!j}"
                ;;
            --query)
                j=$((i + 1))
                query="${!j}"
                ;;
        esac
    done
    if [[ "${query}" == "Role.Arn" ]]; then
        printf 'arn:aws:iam::%s:role/%s\n' "${AWS_TEST_ACCOUNT:-123456789012}" "${role}"
    else
        printf '{"Role":{"Arn":"arn:aws:iam::%s:role/%s"}}\n'             "${AWS_TEST_ACCOUNT:-123456789012}" "${role}"
    fi
    exit 0
fi

if [[ "${service} ${operation}" == "iam put-role-policy" ]]; then
    policy_document=""
    for ((i = 1; i <= $#; i++)); do
        if [[ "${!i}" == "--policy-document" ]]; then
            j=$((i + 1))
            policy_document="${!j}"
        fi
    done
    path="${policy_document#file://}"
    cp "${path}" "${AWS_TEST_POLICY_CAPTURE}"
    exit 0
fi

if [[ "${service} ${operation}" == "iam get-role-policy" ]]; then
    cat "${AWS_TEST_POLICY_CAPTURE}"
    exit 0
fi

echo "unexpected aws invocation: $*" >&2
exit 90
EOF

chmod +x "${BIN}/aws" "${BIN}/npm"

export PATH="${BIN}:${PATH}"
export AWS_TEST_LOG="${LOG}"
export AWS_TEST_POLICY_CAPTURE="${POLICY_CAPTURE}"
export KATCHA_AWS_EXPECTED_ACCOUNT_ID=123456789012
export KATCHA_AWS_BOOTSTRAP_PROFILE=katcha
export KATCHA_REMOTION_LAMBDA_REGION=us-east-1

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
if [[ -s "${LOG}" ]]; then
    echo "FAIL: mismatched Remotion version reached AWS before failing" >&2
    cat "${LOG}" >&2
    exit 1
fi
if ! grep -q 'does not match the pinned renderer dependency' <<<"${mismatch_output}"; then
    echo "FAIL: version mismatch was not reported clearly" >&2
    exit 1
fi
if ! grep -q 'expected: 4.0.529' <<<"${mismatch_output}" || ! grep -q 'actual:   4.0.528' <<<"${mismatch_output}"; then
    echo "FAIL: version mismatch did not report expected and actual versions" >&2
    exit 1
fi

: >"${LOG}"
inspect_output="$(bash "${SCRIPT}")"

if grep -q 'put-role-policy' "${LOG}"; then
    echo "FAIL: inspect mode performed an IAM write" >&2
    exit 1
fi
if ! grep -q 'INSPECT ONLY: no IAM writes were performed.' <<<"${inspect_output}"; then
    echo "FAIL: inspect mode did not report no-write behavior" >&2
    exit 1
fi
if ! grep -q 'policy sha256:' <<<"${inspect_output}"; then
    echo "FAIL: inspect mode did not print policy hash" >&2
    exit 1
fi

: >"${LOG}"
bash "${SCRIPT}" --apply >/dev/null

put_count="$(grep -c 'iam put-role-policy' "${LOG}" || true)"
if [[ "${put_count}" -ne 1 ]]; then
    echo "FAIL: apply mode expected exactly one PutRolePolicy, got ${put_count}" >&2
    exit 1
fi
if [[ ! -s "${POLICY_CAPTURE}" ]]; then
    echo "FAIL: apply mode did not submit a policy document" >&2
    exit 1
fi

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
if grep -q 'put-role-policy' "${LOG}"; then
    echo "FAIL: wrong-account mode performed an IAM write" >&2
    exit 1
fi
if ! grep -q 'AWS account mismatch' <<<"${wrong_output}"; then
    echo "FAIL: wrong-account mode did not report the account mismatch" >&2
    exit 1
fi

echo "PASS: render authorization is review-first, apply-explicit, and account-guarded."
