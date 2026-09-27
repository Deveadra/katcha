#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TMP="$(mktemp -d)"
trap 'rm -rf "${TMP}"' EXIT
mkdir -p "${TMP}/bin"
STATE="${TMP}/state"
LOG="${TMP}/aws.log"
touch "${LOG}"

cat >"${TMP}/bin/aws" <<'MOCK'
#!/usr/bin/env bash
set -euo pipefail
echo "$*" >>"${MOCK_AWS_LOG}"

if [[ "$1 $2" == "sts get-caller-identity" ]]; then
    if [[ "$*" == *"--output json"* ]]; then
        printf '{"Account":"%s","Arn":"arn:aws:iam::%s:role/KatchaTest"}\n' "${MOCK_AWS_ACCOUNT}" "${MOCK_AWS_ACCOUNT}"
        exit 0
    fi
fi

if [[ "$1 $2" == "s3api head-bucket" ]]; then
    [[ -f "${MOCK_AWS_STATE}" ]] && exit 0
    exit 255
fi

if [[ "$1 $2" == "s3api create-bucket" ]]; then
    touch "${MOCK_AWS_STATE}"
    exit 0
fi

case "$1 $2" in
  "s3api put-public-access-block"|"s3api put-bucket-ownership-controls"|"s3api put-bucket-encryption"|"s3api put-bucket-versioning"|"s3api put-bucket-tagging"|"s3api put-bucket-policy")
    exit 0
    ;;
  "s3api get-bucket-location")
    printf '%s\n' "${MOCK_AWS_REGION}"
    exit 0
    ;;
  "s3api get-bucket-versioning")
    if [[ "$*" == *"--query Status"* ]]; then
        echo Enabled
    else
        echo '{"Status":"Enabled"}'
    fi
    exit 0
    ;;
  "s3api get-public-access-block")
    if [[ "$*" == *"--query"* ]]; then
        printf 'True\tTrue\tTrue\tTrue\n'
    else
        echo '{"PublicAccessBlockConfiguration":{"BlockPublicAcls":true,"IgnorePublicAcls":true,"BlockPublicPolicy":true,"RestrictPublicBuckets":true}}'
    fi
    exit 0
    ;;
  "s3api get-bucket-encryption")
    if [[ "$*" == *"--query"* ]]; then
        echo AES256
    else
        echo '{"ServerSideEncryptionConfiguration":{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"AES256"}}]}}'
    fi
    exit 0
    ;;
  "s3api get-bucket-ownership-controls")
    if [[ "$*" == *"--query"* ]]; then
        echo BucketOwnerEnforced
    else
        echo '{"OwnershipControls":{"Rules":[{"ObjectOwnership":"BucketOwnerEnforced"}]}}'
    fi
    exit 0
    ;;
  "s3api get-bucket-policy")
    echo '{"Version":"2012-10-17","Statement":[{"Sid":"DenyInsecureTransport","Effect":"Deny","Principal":"*","Action":"s3:*","Resource":["arn:aws:s3:::test","arn:aws:s3:::test/*"],"Condition":{"Bool":{"aws:SecureTransport":"false"}}}]}'
    exit 0
    ;;
esac

echo "unexpected mock AWS invocation: $*" >&2
exit 90
MOCK
chmod +x "${TMP}/bin/aws"

export PATH="${TMP}/bin:${PATH}"
export MOCK_AWS_LOG="${LOG}"
export MOCK_AWS_STATE="${STATE}"
export MOCK_AWS_ACCOUNT=123456789012
export MOCK_AWS_REGION=us-east-1
export KATCHA_AWS_EXPECTED_ACCOUNT_ID=123456789012
export KATCHA_REMOTION_LAMBDA_REGION=us-east-1

# Default invocation must be read-only.
bash "${ROOT}/scripts/aws_tf_state_bootstrap.sh" >"${TMP}/inspect.out"
if grep -Eq 's3api (create-bucket|put-)' "${LOG}"; then
    echo "inspect mode performed an AWS write" >&2
    cat "${LOG}" >&2
    exit 1
fi
grep -q 'INSPECT ONLY: no AWS writes were performed.' "${TMP}/inspect.out"

# Wrong account must fail before any write.
: >"${LOG}"
export KATCHA_AWS_EXPECTED_ACCOUNT_ID=999999999999
if bash "${ROOT}/scripts/aws_tf_state_bootstrap.sh" --apply >"${TMP}/mismatch.out" 2>"${TMP}/mismatch.err"; then
    echo "account mismatch unexpectedly succeeded" >&2
    exit 1
fi
if grep -Eq 's3api (create-bucket|put-)' "${LOG}"; then
    echo "account mismatch reached an AWS write" >&2
    exit 1
fi
grep -q 'AWS account mismatch' "${TMP}/mismatch.err"

# Correct account apply must create and verify all controls.
: >"${LOG}"
export KATCHA_AWS_EXPECTED_ACCOUNT_ID=123456789012
bash "${ROOT}/scripts/aws_tf_state_bootstrap.sh" --apply >"${TMP}/apply.out"
grep -q 'PASS: Terraform state bucket controls verified.' "${TMP}/apply.out"
for action in     create-bucket     put-public-access-block     put-bucket-ownership-controls     put-bucket-encryption     put-bucket-versioning     put-bucket-tagging     put-bucket-policy; do
    grep -q "${action}" "${LOG}" || {
        echo "expected AWS action missing: ${action}" >&2
        exit 1
    }
done

# An existing bucket in the wrong region must fail before mutation.
: >"${LOG}"
export MOCK_AWS_REGION=us-west-2
if bash "${ROOT}/scripts/aws_tf_state_bootstrap.sh" --apply >"${TMP}/region.out" 2>"${TMP}/region.err"; then
    echo "wrong-region bucket unexpectedly succeeded" >&2
    exit 1
fi
if grep -Eq 's3api put-' "${LOG}"; then
    echo "wrong-region bucket was modified" >&2
    exit 1
fi
grep -q 'state bucket region mismatch' "${TMP}/region.err"

echo "PASS: AWS Terraform state bootstrap is account-guarded, region-guarded, and inspect-only by default."
