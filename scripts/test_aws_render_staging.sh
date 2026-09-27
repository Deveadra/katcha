#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
SCRIPT="${REPO_ROOT}/scripts/aws_render_staging.sh"
TMP="$(mktemp -d)"
trap 'rm -rf "${TMP}"' EXIT

BIN="${TMP}/bin"
ENV_FILE="${TMP}/katcha.env"
LOG="${TMP}/calls.log"
ROLES_DIR="${TMP}/roles-anywhere"
PLAN_PATH="${ROLES_DIR}/terraform-plans/aws-render-staging.tfplan"
mkdir -p "${BIN}"

cat >"${ENV_FILE}" <<'EOF'
KATCHA_RENDER_BACKEND=local
KATCHA_AWS_EXPECTED_ACCOUNT_ID=123456789012
KATCHA_REMOTION_LAMBDA_REGION=us-east-1
KATCHA_AWS_PROFILE=katcha-automation
KATCHA_REMOTION_LAMBDA_MAX_WAIT_MS=1500000
KATCHA_REMOTION_STAGING_PREFIX=katcha-render-staging
EOF

cat >"${BIN}/aws" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
printf 'aws ' >>"${STAGING_TEST_LOG}"
printf '%q ' "$@" >>"${STAGING_TEST_LOG}"
printf '\n' >>"${STAGING_TEST_LOG}"

service="${1:-}"
operation="${2:-}"
account="${STAGING_TEST_ACCOUNT:-123456789012}"

if [[ "${service} ${operation}" == "sts get-caller-identity" ]]; then
    query=""
    output=""
    for ((i = 1; i <= $#; i++)); do
        case "${!i}" in
            --query)
                j=$((i + 1))
                query="${!j}"
                ;;
            --output)
                j=$((i + 1))
                output="${!j}"
                ;;
        esac
    done
    if [[ "${output}" == "json" && -z "${query}" ]]; then
        printf '{"Account":"%s","Arn":"arn:aws:iam::%s:user/bootstrap"}\n' "${account}" "${account}"
        exit 0
    fi
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

if [[ "${service} ${operation}" == "s3api head-bucket" ]]; then
    exit 0
fi

if [[ "${service} ${operation}" == "s3api get-bucket-location" ]]; then
    printf 'None\n'
    exit 0
fi

echo "unexpected aws invocation: $*" >&2
exit 90
EOF

cat >"${BIN}/terraform" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
printf 'terraform ' >>"${STAGING_TEST_LOG}"
printf '%q ' "$@" >>"${STAGING_TEST_LOG}"
printf '\n' >>"${STAGING_TEST_LOG}"

command="${1:-}"
shift || true

case "${command}" in
    init|fmt|validate)
        exit 0
        ;;
    plan)
        out=""
        for arg in "$@"; do
            case "${arg}" in
                -out=*)
                    out="${arg#-out=}"
                    ;;
            esac
        done
        if [[ -z "${out}" ]]; then
            echo "missing -out plan path" >&2
            exit 91
        fi
        mkdir -p "$(dirname "${out}")"
        printf 'katcha-staging-plan-v1\n' >"${out}"
        exit 0
        ;;
    show)
        printf '%s\n' 'Plan: 7 to add, 0 to change, 0 to destroy.'
        printf '%s\n' '  + aws_iam_role_policy.renderer_staging_access'
        exit 0
        ;;
    apply)
        touch "${STAGING_TEST_APPLIED}"
        exit 0
        ;;
    output)
        key="${@: -1}"
        case "${key}" in
            bucket_name)
                printf '%s' 'katcha-render-staging-123456789012-us-east-1'
                ;;
            staging_prefix)
                printf '%s' 'katcha-render-staging'
                ;;
            renderer_role_arn)
                printf '%s' 'arn:aws:iam::123456789012:role/KatchaChronosAutomation'
                ;;
            renderer_staging_policy_name)
                printf '%s' 'KatchaRenderStagingAccess'
                ;;
            *)
                echo "unexpected terraform output key: ${key}" >&2
                exit 92
                ;;
        esac
        exit 0
        ;;
    *)
        echo "unexpected terraform invocation: ${command} $*" >&2
        exit 90
        ;;
esac
EOF

chmod +x "${BIN}/aws" "${BIN}/terraform"

export PATH="${BIN}:${PATH}"
export STAGING_TEST_LOG="${LOG}"
export STAGING_TEST_APPLIED="${TMP}/applied"
export KATCHA_ENV_FILE="${ENV_FILE}"
export KATCHA_AWS_ROLES_ANYWHERE_DIR="${ROLES_DIR}"
export KATCHA_AWS_STAGING_PLAN_PATH="${PLAN_PATH}"
unset KATCHA_AWS_EXPECTED_ACCOUNT_ID
unset KATCHA_REMOTION_LAMBDA_REGION
unset KATCHA_AWS_PROFILE

: >"${LOG}"
plan_output="$(bash "${SCRIPT}")"

if [[ -e "${STAGING_TEST_APPLIED}" ]]; then
    echo "FAIL: default plan mode ran terraform apply" >&2
    exit 1
fi
if grep -q '^terraform apply ' "${LOG}"; then
    echo "FAIL: default plan mode logged terraform apply" >&2
    exit 1
fi
if [[ ! -s "${PLAN_PATH}" ]]; then
    echo "FAIL: plan mode did not create the reviewable plan" >&2
    exit 1
fi
if ! grep -q 'PLAN READY: no Terraform apply was performed.' <<<"${plan_output}"; then
    echo "FAIL: plan mode did not report the no-apply gate" >&2
    exit 1
fi

PLAN_SHA="$(sha256sum "${PLAN_PATH}" | awk '{print $1}')"
if ! grep -q "${PLAN_SHA}" <<<"${plan_output}"; then
    echo "FAIL: plan mode did not print the exact plan SHA" >&2
    exit 1
fi

: >"${LOG}"
set +e
bad_hash_output="$(bash "${SCRIPT}" --apply "$(printf '0%.0s' {1..64})" 2>&1)"
bad_hash_rc=$?
set -e

if [[ "${bad_hash_rc}" -eq 0 ]]; then
    echo "FAIL: mismatched plan SHA unexpectedly succeeded" >&2
    exit 1
fi
if grep -q '^terraform apply ' "${LOG}"; then
    echo "FAIL: mismatched plan SHA reached terraform apply" >&2
    exit 1
fi
if ! grep -q 'Terraform plan SHA-256 mismatch' <<<"${bad_hash_output}"; then
    echo "FAIL: mismatched plan SHA did not report the mismatch" >&2
    exit 1
fi

: >"${LOG}"
rm -f "${STAGING_TEST_APPLIED}"
apply_output="$(bash "${SCRIPT}" --apply "${PLAN_SHA}")"

if [[ ! -e "${STAGING_TEST_APPLIED}" ]]; then
    echo "FAIL: reviewed plan was not applied" >&2
    exit 1
fi
apply_count="$(grep -c '^terraform apply ' "${LOG}" || true)"
if [[ "${apply_count}" -ne 1 ]]; then
    echo "FAIL: expected one terraform apply, got ${apply_count}" >&2
    exit 1
fi

grep -q '^KATCHA_REMOTION_STAGING_BUCKET=katcha-render-staging-123456789012-us-east-1$' "${ENV_FILE}"
grep -q '^KATCHA_REMOTION_STAGING_PREFIX=katcha-render-staging$' "${ENV_FILE}"
grep -q '^KATCHA_REMOTION_STAGING_URL_EXPIRES_SECONDS=3600$' "${ENV_FILE}"
grep -q '^KATCHA_RENDER_BACKEND=local$' "${ENV_FILE}"

if ! grep -q 'backend:      local (unchanged)' <<<"${apply_output}"; then
    echo "FAIL: apply did not prove the backend stayed local" >&2
    exit 1
fi

backup_count="$(find "${ROLES_DIR}/env-backups" -type f -name 'katcha.env.*.bak' | wc -l)"
if [[ "${backup_count}" -lt 1 ]]; then
    echo "FAIL: staging apply did not back up env outside the repository" >&2
    exit 1
fi

: >"${LOG}"
export STAGING_TEST_ACCOUNT=999999999999
set +e
wrong_output="$(bash "${SCRIPT}" --apply "${PLAN_SHA}" 2>&1)"
wrong_rc=$?
set -e
unset STAGING_TEST_ACCOUNT

if [[ "${wrong_rc}" -eq 0 ]]; then
    echo "FAIL: wrong-account staging apply unexpectedly succeeded" >&2
    exit 1
fi
if grep -q '^terraform apply ' "${LOG}"; then
    echo "FAIL: wrong-account mode reached terraform apply" >&2
    exit 1
fi
if ! grep -q 'AWS account mismatch' <<<"${wrong_output}"; then
    echo "FAIL: wrong-account mode did not report the mismatch" >&2
    exit 1
fi

echo "PASS: staging provisioning is plan-first, hash-gated, account-guarded, and backend-safe."
