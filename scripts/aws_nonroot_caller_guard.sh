#!/usr/bin/env bash
# Fail closed when an AWS SDK/CLI identity is root or cannot be identified.
# Use only after STS GetCallerIdentity and the expected-account check.
set -euo pipefail

if (( $# != 1 )); then
    echo "BLOCKED: expected exactly one verified AWS caller ARN." >&2
    exit 3
fi

CALLER_ARN="$1"
if [[ ! "${CALLER_ARN}" =~ ^arn:[a-z0-9-]+:(iam|sts)::[0-9]{12}:.+ ]]; then
    echo "BLOCKED: AWS caller ARN is missing or unrecognized; refusing Katcha AWS operations." >&2
    exit 3
fi

if [[ "${CALLER_ARN}" =~ ^arn:[a-z0-9-]+:iam::[0-9]{12}:root$ ]]; then
    echo "BLOCKED: Katcha AWS operations cannot run as the AWS account root user." >&2
    echo "Use a tested MFA-protected administrative session with temporary credentials." >&2
    echo "Configure KATCHA_AWS_BOOTSTRAP_PROFILE for IAM/Terraform administration," >&2
    echo "or KATCHA_AWS_PROFILE for a read-only/durable AWS workload." >&2
    echo "See docs/AWS_BOOTSTRAP_IDENTITY_MIGRATION.md for a safe migration." >&2
    echo "No AWS resource changes were attempted by this guard." >&2
    exit 3
fi
