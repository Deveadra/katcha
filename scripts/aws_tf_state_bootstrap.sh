#!/usr/bin/env bash
set -euo pipefail

MODE="inspect"
if [[ "${1:-}" == "--apply" ]]; then
    MODE="apply"
elif [[ "${1:-}" == "--help" || "${1:-}" == "-h" ]]; then
    cat <<'EOF'
Usage: bash scripts/aws_tf_state_bootstrap.sh [--apply]

Default mode is inspect-only and performs no writes.

Required environment:
  KATCHA_AWS_EXPECTED_ACCOUNT_ID   exact 12-digit AWS account ID
  KATCHA_REMOTION_LAMBDA_REGION   AWS region
Optional:
  KATCHA_TERRAFORM_STATE_BUCKET   override deterministic state bucket name
  AWS_PROFILE                     AWS profile / SSO profile
EOF
    exit 0
elif [[ $# -gt 0 ]]; then
    echo "ERROR: unsupported argument: $1" >&2
    exit 2
fi

EXPECTED_ACCOUNT="${KATCHA_AWS_EXPECTED_ACCOUNT_ID:-}"
REGION="${KATCHA_REMOTION_LAMBDA_REGION:-}"

if [[ ! "${EXPECTED_ACCOUNT}" =~ ^[0-9]{12}$ ]]; then
    echo "ERROR: KATCHA_AWS_EXPECTED_ACCOUNT_ID must be exactly 12 digits." >&2
    exit 2
fi
if [[ -z "${REGION}" ]]; then
    echo "ERROR: KATCHA_REMOTION_LAMBDA_REGION must be set explicitly." >&2
    exit 2
fi
for command in aws python3; do
    if ! command -v "${command}" >/dev/null 2>&1; then
        echo "ERROR: required command is not installed: ${command}" >&2
        exit 2
    fi
done

BUCKET="${KATCHA_TERRAFORM_STATE_BUCKET:-katcha-tfstate-${EXPECTED_ACCOUNT}-${REGION}}"
if [[ ! "${BUCKET}" =~ ^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$ ]] || [[ "${BUCKET}" == *".."* ]]; then
    echo "ERROR: invalid Terraform state bucket name: ${BUCKET}" >&2
    exit 2
fi

IDENTITY_JSON="$(aws sts get-caller-identity --output json)"
ACTUAL_ACCOUNT="$(python3 -c 'import json,sys; print(json.load(sys.stdin)["Account"])' <<<"${IDENTITY_JSON}")"
CALLER_ARN="$(python3 -c 'import json,sys; print(json.load(sys.stdin)["Arn"])' <<<"${IDENTITY_JSON}")"

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
echo "  region:  ${REGION}"
echo "  state bucket: ${BUCKET}"

normalize_bucket_region() {
    local value="$1"
    if [[ "${value}" == "None" || "${value}" == "null" || -z "${value}" ]]; then
        echo "us-east-1"
    else
        echo "${value}"
    fi
}

verify_bucket_region() {
    local actual
    actual="$(aws s3api get-bucket-location --bucket "${BUCKET}" --query LocationConstraint --output text)"
    actual="$(normalize_bucket_region "${actual}")"
    if [[ "${actual}" != "${REGION}" ]]; then
        echo "ERROR: state bucket region mismatch." >&2
        echo "  expected: ${REGION}" >&2
        echo "  actual:   ${actual}" >&2
        exit 4
    fi
    echo "${actual}"
}

if [[ "${MODE}" == "inspect" ]]; then
    if aws s3api head-bucket --bucket "${BUCKET}" >/dev/null 2>&1; then
        echo "Existing bucket is reachable. Current controls:"
        echo "  bucket region: $(verify_bucket_region)"
        aws s3api get-bucket-versioning --bucket "${BUCKET}" --output json || true
        aws s3api get-public-access-block --bucket "${BUCKET}" --output json || true
        aws s3api get-bucket-encryption --bucket "${BUCKET}" --output json || true
        aws s3api get-bucket-ownership-controls --bucket "${BUCKET}" --output json || true
    else
        echo "State bucket does not exist or is not reachable."
    fi
    cat <<EOF
INSPECT ONLY: no AWS writes were performed.

With --apply this script will ensure:
  - private S3 bucket: ${BUCKET}
  - BucketOwnerEnforced ownership
  - Block Public Access (all controls)
  - SSE-S3 encryption
  - versioning enabled
  - HTTPS-only bucket policy
  - project/component tags

Terraform backend:
  bucket       = ${BUCKET}
  key          = katcha/aws-render-staging/terraform.tfstate
  region       = ${REGION}
  encrypt      = true
  use_lockfile = true
EOF
    exit 0
fi

if ! aws s3api head-bucket --bucket "${BUCKET}" >/dev/null 2>&1; then
    echo "Creating state bucket ${BUCKET}..."
    if [[ "${REGION}" == "us-east-1" ]]; then
        aws s3api create-bucket --bucket "${BUCKET}" --region "${REGION}" >/dev/null
    else
        aws s3api create-bucket           --bucket "${BUCKET}"           --region "${REGION}"           --create-bucket-configuration "LocationConstraint=${REGION}" >/dev/null
    fi
else
    echo "State bucket already exists and is reachable."
    echo "Verified existing bucket region: $(verify_bucket_region)"
fi

aws s3api put-public-access-block   --bucket "${BUCKET}"   --public-access-block-configuration '{"BlockPublicAcls":true,"IgnorePublicAcls":true,"BlockPublicPolicy":true,"RestrictPublicBuckets":true}'

aws s3api put-bucket-ownership-controls   --bucket "${BUCKET}"   --ownership-controls 'Rules=[{ObjectOwnership=BucketOwnerEnforced}]'

aws s3api put-bucket-encryption   --bucket "${BUCKET}"   --server-side-encryption-configuration '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"AES256"}}]}'

aws s3api put-bucket-versioning   --bucket "${BUCKET}"   --versioning-configuration Status=Enabled

aws s3api put-bucket-tagging   --bucket "${BUCKET}"   --tagging 'TagSet=[{Key=Project,Value=katcha},{Key=Component,Value=terraform-state},{Key=ManagedBy,Value=bootstrap-script}]'

POLICY_FILE="$(mktemp)"
trap 'rm -f "${POLICY_FILE}"' EXIT
cat >"${POLICY_FILE}" <<EOF
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "DenyInsecureTransport",
      "Effect": "Deny",
      "Principal": "*",
      "Action": "s3:*",
      "Resource": [
        "arn:aws:s3:::${BUCKET}",
        "arn:aws:s3:::${BUCKET}/*"
      ],
      "Condition": {
        "Bool": {
          "aws:SecureTransport": "false"
        }
      }
    }
  ]
}
EOF

aws s3api put-bucket-policy   --bucket "${BUCKET}"   --policy "file://${POLICY_FILE}"

BUCKET_REGION="$(verify_bucket_region)"

VERSIONING="$(aws s3api get-bucket-versioning --bucket "${BUCKET}" --query Status --output text)"
if [[ "${VERSIONING}" != "Enabled" ]]; then
    echo "ERROR: state bucket versioning verification failed." >&2
    exit 4
fi

PUBLIC_BLOCK="$(aws s3api get-public-access-block --bucket "${BUCKET}" --query 'PublicAccessBlockConfiguration.[BlockPublicAcls,IgnorePublicAcls,BlockPublicPolicy,RestrictPublicBuckets]' --output text)"
if [[ "${PUBLIC_BLOCK}" != 
echo "Configure Terraform with:"
echo "  bucket=${BUCKET}"
echo "  key=katcha/aws-render-staging/terraform.tfstate"
echo "  region=${REGION}"
echo "  encrypt=true"
echo "  use_lockfile=true"
True\tTrue\tTrue\tTrue' ]]; then
    echo "ERROR: state bucket public-access-block verification failed: ${PUBLIC_BLOCK}" >&2
    exit 4
fi

OWNERSHIP="$(aws s3api get-bucket-ownership-controls --bucket "${BUCKET}" --query 'OwnershipControls.Rules[0].ObjectOwnership' --output text)"
if [[ "${OWNERSHIP}" != "BucketOwnerEnforced" ]]; then
    echo "ERROR: state bucket ownership verification failed: ${OWNERSHIP}" >&2
    exit 4
fi

ENCRYPTION="$(aws s3api get-bucket-encryption --bucket "${BUCKET}" --query 'ServerSideEncryptionConfiguration.Rules[0].ApplyServerSideEncryptionByDefault.SSEAlgorithm' --output text)"
if [[ "${ENCRYPTION}" != "AES256" ]]; then
    echo "ERROR: state bucket encryption verification failed: ${ENCRYPTION}" >&2
    exit 4
fi

POLICY_JSON="$(aws s3api get-bucket-policy --bucket "${BUCKET}" --query Policy --output text)"
POLICY_OK="$(POLICY_JSON="${POLICY_JSON}" python3 -c '
import json
import os

policy = json.loads(os.environ["POLICY_JSON"])
ok = False
for statement in policy.get("Statement", []):
    condition = statement.get("Condition", {})
    secure = condition.get("Bool", {}).get("aws:SecureTransport")
    if (
        statement.get("Effect") == "Deny"
        and statement.get("Action") == "s3:*"
        and str(secure).lower() == "false"
    ):
        ok = True
        break
print("true" if ok else "false")
')"
if [[ "${POLICY_OK}" != "true" ]]; then
    echo "ERROR: HTTPS-only state bucket policy verification failed." >&2
    exit 4
fi

echo "PASS: Terraform state bucket controls verified."
echo "  region:      ${BUCKET_REGION}"
echo "  versioning:  ${VERSIONING}"
echo "  ownership:   ${OWNERSHIP}"
echo "  encryption:  ${ENCRYPTION}"
echo "  public:      blocked"
echo "  transport:   HTTPS-only"
echo "Configure Terraform with:"
echo "  bucket=${BUCKET}"
echo "  key=katcha/aws-render-staging/terraform.tfstate"
echo "  region=${REGION}"
echo "  encrypt=true"
echo "  use_lockfile=true"
