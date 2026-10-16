#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
GUARD="${ROOT}/scripts/aws_nonroot_caller_guard.sh"

# Tested assumed roles and named IAM principals remain usable.
bash "${GUARD}" "arn:aws:sts::123456789012:assumed-role/KatchaChronosAutomation/session"
bash "${GUARD}" "arn:aws:sts::123456789012:assumed-role/AWSReservedSSO_KatchaBootstrap_001/session"
bash "${GUARD}" "arn:aws:iam::123456789012:user/temporary-bootstrap"

for arn in \
    "arn:aws:iam::123456789012:root" \
    "arn:aws-us-gov:iam::123456789012:root" \
    "arn:aws-cn:iam::123456789012:root" \
    "" \
    "malformed"; do
    output="$(bash "${GUARD}" "${arn}" 2>&1)" && {
        echo "FAIL: unsafe AWS principal accepted" >&2
        exit 1
    }
    if ! grep -q 'BLOCKED:' <<<"${output}"; then
        echo "FAIL: unsafe AWS principal lacked actionable rejection" >&2
        exit 1
    fi
done

if bash "${GUARD}" >/dev/null 2>&1; then
    echo "FAIL: missing caller ARN accepted" >&2
    exit 1
fi
echo "PASS: AWS root bootstrap caller rejected in all partitions; temporary-role identities accepted."
