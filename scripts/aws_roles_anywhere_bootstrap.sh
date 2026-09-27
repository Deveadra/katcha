#!/usr/bin/env bash
set -euo pipefail

APPLY=false
case "${1:-}" in
    "")
        ;;
    --apply)
        APPLY=true
        ;;
    --help|-h)
        cat <<'EOF'
Usage:
  bash scripts/aws_roles_anywhere_bootstrap.sh
  bash scripts/aws_roles_anywhere_bootstrap.sh --apply

The default mode is inspect-only. It verifies the currently authenticated bootstrap
identity and prints the exact local/AWS resources that would be created.

Required environment:
  KATCHA_AWS_EXPECTED_ACCOUNT_ID   Exact 12-digit AWS account ID.

Optional environment:
  KATCHA_AWS_BOOTSTRAP_PROFILE     Interactive/admin profile used once for setup.
                                   Default: katcha
  KATCHA_AWS_AUTOMATION_PROFILE    Durable local profile to create.
                                   Default: katcha-automation
  KATCHA_REMOTION_LAMBDA_REGION    AWS region. Default: us-east-1
  KATCHA_AWS_ROLES_ANYWHERE_DIR    Local certificate directory.
                                   Default: ~/.aws/katcha-roles-anywhere
  KATCHA_AWS_ROLES_ANYWHERE_CN     Workload certificate common name.
                                   Default: katcha-chronos
EOF
        exit 0
        ;;
    *)
        echo "ERROR: unsupported argument: $1" >&2
        exit 2
        ;;
esac

EXPECTED_ACCOUNT="${KATCHA_AWS_EXPECTED_ACCOUNT_ID:-}"
BOOTSTRAP_PROFILE="${KATCHA_AWS_BOOTSTRAP_PROFILE:-katcha}"
AUTOMATION_PROFILE="${KATCHA_AWS_AUTOMATION_PROFILE:-katcha-automation}"
REGION="${KATCHA_REMOTION_LAMBDA_REGION:-us-east-1}"
ROLES_DIR="${KATCHA_AWS_ROLES_ANYWHERE_DIR:-$HOME/.aws/katcha-roles-anywhere}"
CLIENT_CN="${KATCHA_AWS_ROLES_ANYWHERE_CN:-katcha-chronos}"

TRUST_ANCHOR_NAME="katcha-chronos"
ROLE_NAME="KatchaChronosAutomation"
RA_PROFILE_NAME="katcha-chronos"

HELPER_VERSION="1.8.5"
HELPER_PATH="${HOME}/.local/bin/aws_signing_helper"

CA_DIR="${ROLES_DIR}/ca"
RUNTIME_DIR="${ROLES_DIR}/runtime"
CA_KEY="${CA_DIR}/root-ca-key.pem"
CA_CERT="${CA_DIR}/root-ca.pem"
CLIENT_KEY="${RUNTIME_DIR}/client-key.pem"
CLIENT_CSR="${RUNTIME_DIR}/client.csr"
CLIENT_CERT="${RUNTIME_DIR}/client.pem"
CLIENT_EXT="${RUNTIME_DIR}/client-ext.cnf"

if [[ ! "${EXPECTED_ACCOUNT}" =~ ^[0-9]{12}$ ]]; then
    echo "ERROR: KATCHA_AWS_EXPECTED_ACCOUNT_ID must be the intended 12-digit account ID." >&2
    exit 2
fi

if [[ -z "${REGION}" ]]; then
    echo "ERROR: KATCHA_REMOTION_LAMBDA_REGION must not be empty." >&2
    exit 2
fi

for command in aws openssl curl sha256sum python3 install uname; do
    if ! command -v "${command}" >/dev/null 2>&1; then
        echo "ERROR: required command is not installed: ${command}" >&2
        exit 2
    fi
done

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

case "$(uname -m)" in
    x86_64|amd64)
        HELPER_ARCH="X86_64"
        HELPER_SHA256="beec9ed1c492d93db809890f16713e3556353294b823c2184ad4e891f1b2b54d"
        ;;
    aarch64|arm64)
        HELPER_ARCH="Aarch64"
        HELPER_SHA256="3d131aa888cd56da446f9c6bb460b1f0569f6c7edc74eae6193a2fe3928883ba"
        ;;
    *)
        echo "ERROR: unsupported architecture for pinned AWS signing helper: $(uname -m)" >&2
        exit 2
        ;;
esac

HELPER_URL="https://rolesanywhere.amazonaws.com/releases/${HELPER_VERSION}/${HELPER_ARCH}/Linux/Amzn2023/aws_signing_helper"

echo "AWS identity verified:"
echo "  account:             ${ACTUAL_ACCOUNT}"
echo "  caller:              ${CALLER_ARN}"
echo "  region:              ${REGION}"
echo
echo "Planned durable-auth resources:"
echo "  AWS bootstrap profile: ${BOOTSTRAP_PROFILE}"
echo "  AWS runtime profile:   ${AUTOMATION_PROFILE}"
echo "  trust anchor:          ${TRUST_ANCHOR_NAME}"
echo "  IAM role:              ${ROLE_NAME}"
echo "  Roles Anywhere profile:${RA_PROFILE_NAME}"
echo "  certificate subject:   CN=${CLIENT_CN}"
echo "  certificate directory: ${ROLES_DIR}"
echo "  signing helper:        ${HELPER_PATH}"
echo "  helper release:        ${HELPER_VERSION}"
echo
echo "The IAM role is created with NO resource permissions."
echo "Remotion/S3 permissions remain a separate review-and-apply step."

if [[ "${APPLY}" != "true" ]]; then
    echo
    echo "INSPECT ONLY: no local files or AWS resources were created."
    echo "Run again with --apply only after the account and resource names above are correct."
    exit 0
fi

umask 077
mkdir -p "${CA_DIR}" "${RUNTIME_DIR}" "$(dirname "${HELPER_PATH}")" "${HOME}/.aws"
chmod 700 "${ROLES_DIR}" "${CA_DIR}" "${RUNTIME_DIR}" "${HOME}/.aws"

cert_files=("${CA_KEY}" "${CA_CERT}" "${CLIENT_KEY}" "${CLIENT_CERT}")
present=0
for path in "${cert_files[@]}"; do
    if [[ -e "${path}" ]]; then
        present=$((present + 1))
    fi
done

if (( present != 0 && present != ${#cert_files[@]} )); then
    echo "ERROR: partial certificate state exists under ${ROLES_DIR}." >&2
    echo "Refusing to overwrite or guess. Reconcile those files before retrying." >&2
    exit 4
fi

if (( present == 0 )); then
    echo "Generating local CA and workload certificate..."
    openssl genrsa -out "${CA_KEY}" 4096
    openssl req         -x509         -new         -sha256         -key "${CA_KEY}"         -days 3650         -out "${CA_CERT}"         -subj "/CN=Katcha Chronos Local Root CA"         -addext "basicConstraints=critical,CA:TRUE,pathlen:0"         -addext "keyUsage=critical,keyCertSign,cRLSign"

    openssl genrsa -out "${CLIENT_KEY}" 3072
    openssl req         -new         -sha256         -key "${CLIENT_KEY}"         -out "${CLIENT_CSR}"         -subj "/CN=${CLIENT_CN}"

    cat >"${CLIENT_EXT}" <<'EOF'
basicConstraints=critical,CA:FALSE
keyUsage=critical,digitalSignature,keyEncipherment
extendedKeyUsage=clientAuth
subjectKeyIdentifier=hash
authorityKeyIdentifier=keyid,issuer
EOF

    openssl x509         -req         -sha256         -in "${CLIENT_CSR}"         -CA "${CA_CERT}"         -CAkey "${CA_KEY}"         -CAcreateserial         -days 365         -out "${CLIENT_CERT}"         -extfile "${CLIENT_EXT}"

    rm -f "${CLIENT_CSR}" "${CLIENT_EXT}"
    chmod 600 "${CA_KEY}" "${CLIENT_KEY}"
    chmod 644 "${CA_CERT}" "${CLIENT_CERT}"
else
    echo "Reusing complete certificate set under ${ROLES_DIR}."
fi

echo "Installing pinned AWS IAM Roles Anywhere credential helper..."
tmp_helper="$(mktemp)"
trap 'rm -f "${tmp_helper}" "${trust_policy_file:-}"' EXIT
curl -fsSL "${HELPER_URL}" -o "${tmp_helper}"
echo "${HELPER_SHA256}  ${tmp_helper}" | sha256sum -c -
install -m 0755 "${tmp_helper}" "${HELPER_PATH}"

TRUST_ANCHOR_ARN="$(
    aws rolesanywhere list-trust-anchors         --profile "${BOOTSTRAP_PROFILE}"         --region "${REGION}"         --query "trustAnchors[?name=='${TRUST_ANCHOR_NAME}'] | [0].trustAnchorArn"         --output text
)"
if [[ -z "${TRUST_ANCHOR_ARN}" || "${TRUST_ANCHOR_ARN}" == "None" ]]; then
    echo "Creating IAM Roles Anywhere trust anchor..."
    source_json="$(
        python3 - "${CA_CERT}" <<'PY'
import json
from pathlib import Path
import sys

certificate = Path(sys.argv[1]).read_text(encoding="utf-8")
print(json.dumps({
    "sourceType": "CERTIFICATE_BUNDLE",
    "sourceData": {"x509CertificateData": certificate},
}))
PY
    )"
    TRUST_ANCHOR_ARN="$(
        aws rolesanywhere create-trust-anchor             --profile "${BOOTSTRAP_PROFILE}"             --region "${REGION}"             --name "${TRUST_ANCHOR_NAME}"             --source "${source_json}"             --enabled             --query trustAnchor.trustAnchorArn             --output text
    )"
else
    echo "Reusing trust anchor: ${TRUST_ANCHOR_ARN}"
fi

ROLE_ARN="$(
    aws iam get-role         --profile "${BOOTSTRAP_PROFILE}"         --role-name "${ROLE_NAME}"         --query Role.Arn         --output text 2>/dev/null || true
)"
if [[ -z "${ROLE_ARN}" || "${ROLE_ARN}" == "None" ]]; then
    echo "Creating dedicated IAM role with certificate- and trust-anchor-bound trust policy..."
    trust_policy_file="$(mktemp)"
    python3 - "${trust_policy_file}" "${TRUST_ANCHOR_ARN}" "${EXPECTED_ACCOUNT}" "${CLIENT_CN}" <<'PY'
import json
from pathlib import Path
import sys

path, trust_anchor_arn, account_id, common_name = sys.argv[1:]
policy = {
    "Version": "2012-10-17",
    "Statement": [{
        "Effect": "Allow",
        "Principal": {"Service": "rolesanywhere.amazonaws.com"},
        "Action": [
            "sts:AssumeRole",
            "sts:TagSession",
            "sts:SetSourceIdentity",
        ],
        "Condition": {
            "ArnEquals": {"aws:SourceArn": trust_anchor_arn},
            "StringEquals": {
                "aws:SourceAccount": account_id,
                "aws:PrincipalTag/x509Subject/CN": common_name,
            },
        },
    }],
}
Path(path).write_text(json.dumps(policy, indent=2), encoding="utf-8")
PY

    ROLE_ARN="$(
        aws iam create-role             --profile "${BOOTSTRAP_PROFILE}"             --role-name "${ROLE_NAME}"             --description "Katcha unattended renderer authentication from Chronos via IAM Roles Anywhere"             --assume-role-policy-document "file://${trust_policy_file}"             --query Role.Arn             --output text
    )"
else
    echo "Reusing IAM role: ${ROLE_ARN}"
fi

RA_PROFILE_ARN="$(
    aws rolesanywhere list-profiles         --profile "${BOOTSTRAP_PROFILE}"         --region "${REGION}"         --query "profiles[?name=='${RA_PROFILE_NAME}'] | [0].profileArn"         --output text
)"
if [[ -z "${RA_PROFILE_ARN}" || "${RA_PROFILE_ARN}" == "None" ]]; then
    echo "Creating IAM Roles Anywhere profile..."
    RA_PROFILE_ARN="$(
        aws rolesanywhere create-profile             --profile "${BOOTSTRAP_PROFILE}"             --region "${REGION}"             --name "${RA_PROFILE_NAME}"             --role-arns "${ROLE_ARN}"             --duration-seconds 3600             --enabled             --query profile.profileArn             --output text
    )"
else
    echo "Reusing Roles Anywhere profile: ${RA_PROFILE_ARN}"
fi

if [[ -f "${HOME}/.aws/config" ]]; then
    backup="${HOME}/.aws/config.bak.$(date +%Y%m%d%H%M%S)"
    cp -a "${HOME}/.aws/config" "${backup}"
    chmod 600 "${backup}"
    echo "Backed up AWS config to ${backup}"
fi

for path in "${HELPER_PATH}" "${CLIENT_CERT}" "${CLIENT_KEY}"; do
    if [[ "${path}" =~ [[:space:]] ]]; then
        echo "ERROR: credential_process paths containing whitespace are not supported by this bootstrap." >&2
        exit 5
    fi
done

credential_process="${HELPER_PATH} credential-process --certificate ${CLIENT_CERT} --private-key ${CLIENT_KEY} --trust-anchor-arn ${TRUST_ANCHOR_ARN} --profile-arn ${RA_PROFILE_ARN} --role-arn ${ROLE_ARN}"

aws configure set region "${REGION}" --profile "${AUTOMATION_PROFILE}"
aws configure set output json --profile "${AUTOMATION_PROFILE}"
aws configure set credential_process "${credential_process}" --profile "${AUTOMATION_PROFILE}"
chmod 600 "${HOME}/.aws/config"

echo "Validating durable credential_process profile..."
DURABLE_ACCOUNT="$(
    aws sts get-caller-identity         --profile "${AUTOMATION_PROFILE}"         --region "${REGION}"         --query Account         --output text
)"
DURABLE_ARN="$(
    aws sts get-caller-identity         --profile "${AUTOMATION_PROFILE}"         --region "${REGION}"         --query Arn         --output text
)"

if [[ "${DURABLE_ACCOUNT}" != "${EXPECTED_ACCOUNT}" ]]; then
    echo "ERROR: durable profile resolved to the wrong AWS account." >&2
    echo "  expected: ${EXPECTED_ACCOUNT}" >&2
    echo "  actual:   ${DURABLE_ACCOUNT}" >&2
    exit 6
fi

echo
echo "PASS: durable AWS authentication is configured."
echo "  profile:      ${AUTOMATION_PROFILE}"
echo "  account:      ${DURABLE_ACCOUNT}"
echo "  caller:       ${DURABLE_ARN}"
echo "  role:         ${ROLE_ARN}"
echo "  trust anchor: ${TRUST_ANCHOR_ARN}"
echo "  RA profile:   ${RA_PROFILE_ARN}"
echo
echo "Runtime exports for Katcha:"
echo "  export AWS_PROFILE=${AUTOMATION_PROFILE}"
echo "  export KATCHA_AWS_SIGNING_HELPER_PATH=${HELPER_PATH}"
echo "  export KATCHA_AWS_CERT_PATH=${CLIENT_CERT}"
echo "  export KATCHA_AWS_PRIVATE_KEY_PATH=${CLIENT_KEY}"
echo
echo "No Remotion/S3 permissions were attached to the role."
echo "Review and apply those permissions separately before cloud rendering."
