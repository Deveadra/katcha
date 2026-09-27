#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
SCRIPT="${REPO_ROOT}/scripts/aws_render_staging_deploy.sh"
TMP="$(mktemp -d)"
trap 'rm -rf "${TMP}"' EXIT

mkdir -p "${TMP}/bin" "${TMP}/home/.aws/katcha-roles-anywhere"
LOG="${TMP}/calls.log"
ENV_FILE="${TMP}/katcha.env"

cat >"${ENV_FILE}" <<'EOF'
KATCHA_AWS_EXPECTED_ACCOUNT_ID=123456789012
KATCHA_REMOTION_LAMBDA_REGION=us-east-1
KATCHA_RENDER_BACKEND=local
KATCHA_REMOTION_STAGING_PREFIX=katcha-render-staging
EOF

cat >"${TMP}/bin/aws" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
echo "aws $*" >>"${MOCK_LOG}"

if [[ "$*" == *"sts get-caller-identity"* && "$*" == *"--query Account"* ]]; then
    echo "123456789012"
elif [[ "$*" == *"sts get-caller-identity"* && "$*" == *"--query Arn"* ]]; then
    echo "arn:aws:iam::123456789012:user/bootstrap"
elif [[ "$*" == *"s3api head-bucket"* ]]; then
    exit 0
elif [[ "$*" == *"s3api get-bucket-location"* ]]; then
    echo "None"
elif [[ "$*" == *"s3api get-public-access-block"* ]]; then
    echo '{"PublicAccessBlockConfiguration":{"BlockPublicAcls":true,"IgnorePublicAcls":true,"BlockPublicPolicy":true,"RestrictPublicBuckets":true}}'
elif [[ "$*" == *"s3api get-bucket-ownership-controls"* ]]; then
    echo "BucketOwnerEnforced"
elif [[ "$*" == *"s3api get-bucket-encryption"* ]]; then
    echo "AES256"
elif [[ "$*" == *"s3api get-bucket-lifecycle-configuration"* ]]; then
    echo '{"Rules":[{"Status":"Enabled","Filter":{"Prefix":"katcha-render-staging/"},"Expiration":{"Days":1}}]}'
elif [[ "$*" == *"s3api get-bucket-policy"* ]]; then
    echo '{"Version":"2012-10-17","Statement":[{"Effect":"Deny","Action":"s3:*","Condition":{"Bool":{"aws:SecureTransport":"false"}}}]}'
elif [[ "$*" == *"iam get-role-policy"* ]]; then
    echo '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Action":["s3:GetObject"],"Resource":["arn:aws:s3:::katcha-render-staging-123456789012-us-east-1/katcha-render-staging/*"]}]}'
else
    echo "unexpected aws invocation: $*" >&2
    exit 91
fi
EOF
chmod +x "${TMP}/bin/aws"

cat >"${TMP}/bin/terraform" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
echo "terraform $*" >>"${MOCK_LOG}"

args=("$@")
index=0
if [[ "${args[0]:-}" == -chdir=* ]]; then
    index=1
fi
command="${args[${index}]:-}"
case "${command}" in
    init|fmt|validate)
        exit 0
        ;;
    plan)
        for arg in "$@"; do
            if [[ "${arg}" == -out=* ]]; then
                : >"${arg#-out=}"
            fi
        done
        echo "Plan: 7 to add, 0 to change, 0 to destroy."
        ;;
    show)
        echo "mock reviewed plan"
        ;;
    apply)
        exit 0
        ;;
    output)
        name="${args[-1]}"
        case "${name}" in
            bucket_name)
                echo "katcha-render-staging-123456789012-us-east-1"
                ;;
            staging_prefix)
                echo "katcha-render-staging"
                ;;
            renderer_role_arn)
                echo "arn:aws:iam::123456789012:role/KatchaChronosAutomation"
                ;;
            renderer_staging_policy_name)
                echo "KatchaRenderStagingAccess"
                ;;
            renderer_iam_policy_json)
                echo '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Action":["s3:GetObject"],"Resource":["arn:aws:s3:::katcha-render-staging-123456789012-us-east-1/katcha-render-staging/*"]}]}'
                ;;
            *)
                echo "unexpected terraform output: ${name}" >&2
                exit 92
                ;;
        esac
        ;;
    *)
        echo "unexpected terraform command: ${command}" >&2
        exit 93
        ;;
esac
EOF
chmod +x "${TMP}/bin/terraform"

export PATH="${TMP}/bin:${PATH}"
export HOME="${TMP}/home"
export MOCK_LOG="${LOG}"
export KATCHA_ENV_FILE="${ENV_FILE}"
export KATCHA_AWS_EXPECTED_ACCOUNT_ID=123456789012
export KATCHA_REMOTION_LAMBDA_REGION=us-east-1

echo "Testing inspect mode..."
: >"${LOG}"
bash "${SCRIPT}" >"${TMP}/inspect.out"

grep -q "INSPECT ONLY" "${TMP}/inspect.out"
grep -q "terraform .* plan " "${LOG}"
if grep -q "terraform .* apply " "${LOG}"; then
    echo "ERROR: inspect mode invoked terraform apply" >&2
    exit 1
fi
if grep -q '^KATCHA_REMOTION_STAGING_BUCKET=' "${ENV_FILE}"; then
    echo "ERROR: inspect mode mutated staging metadata" >&2
    exit 1
fi

echo "Testing apply mode..."
: >"${LOG}"
bash "${SCRIPT}" --apply >"${TMP}/apply.out"

grep -q "PASS: private render-staging infrastructure is applied and verified." "${TMP}/apply.out"
grep -q "terraform .* apply " "${LOG}"
grep -q '^KATCHA_REMOTION_STAGING_BUCKET=katcha-render-staging-123456789012-us-east-1$' "${ENV_FILE}"
grep -q '^KATCHA_REMOTION_STAGING_PREFIX=katcha-render-staging$' "${ENV_FILE}"
grep -q '^KATCHA_RENDER_BACKEND=local$' "${ENV_FILE}"

echo "PASS: AWS render-staging deploy wrapper is review-first and persists only verified metadata."
