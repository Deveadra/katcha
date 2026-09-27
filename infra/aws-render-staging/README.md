# Katcha cloud-render staging bucket

This Terraform root creates the private S3 staging bucket used by the hybrid
local-MinIO -> AWS Remotion Lambda render path and attaches one dedicated,
prefix-scoped inline S3 policy to the existing durable `KatchaChronosAutomation`
role.

It does not deploy Remotion Lambda functions, create IAM users, create AWS access
keys, or modify the Katcha database.

## Safety properties

- requires an explicit 12-digit `expected_account_id`;
- refuses to manage resources when the active AWS account differs;
- Block Public Access enabled on all four controls;
- BucketOwnerEnforced object ownership;
- SSE-S3 encryption;
- HTTPS-only bucket policy;
- staged objects expire automatically after one day by default;
- incomplete multipart uploads are aborted after one day;
- `force_destroy=false`, so Terraform cannot silently delete a bucket containing objects;
- resolves one existing durable renderer role, defaulting to `KatchaChronosAutomation`;
- attaches only the generated staging S3 policy as `KatchaRenderStagingAccess`;
- keeps Remotion control-plane permissions in the separate `KatchaRemotionControlPlane` policy;
- repeats the expected-account guard on the IAM attachment itself.

## Bootstrap remote state first

Terraform state must not live only on the operator workstation.

From the repository root, use the guarded bootstrap script. The default invocation is
inspect-only and performs no writes:

```bash
export KATCHA_AWS_EXPECTED_ACCOUNT_ID=123456789012
export KATCHA_REMOTION_LAMBDA_REGION=us-east-1

bash scripts/aws_tf_state_bootstrap.sh
```

After reviewing the reported account, caller, region, bucket name, and intended controls,
create/repair only the Terraform state bucket:

```bash
bash scripts/aws_tf_state_bootstrap.sh --apply
```

The state bucket has versioning, SSE-S3, Block Public Access, BucketOwnerEnforced ownership,
and an HTTPS-only bucket policy. Terraform uses native S3 lockfiles rather than DynamoDB.

Initialize this root with partial backend configuration using the authorized
bootstrap/admin profile. The durable renderer role should not be granted
infrastructure-management permissions just to provision its own bucket access:

```bash
cd ~/src/katcha/infra/aws-render-staging

export AWS_PROFILE=katcha
STATE_BUCKET="katcha-tfstate-${KATCHA_AWS_EXPECTED_ACCOUNT_ID}-${KATCHA_REMOTION_LAMBDA_REGION}"

terraform init -reconfigure \
  -backend-config="bucket=${STATE_BUCKET}" \
  -backend-config="key=katcha/aws-render-staging/terraform.tfstate" \
  -backend-config="region=${KATCHA_REMOTION_LAMBDA_REGION}" \
  -backend-config="encrypt=true" \
  -backend-config="use_lockfile=true"

terraform fmt -check
terraform validate

terraform plan \
  -var='expected_account_id=123456789012' \
  -var='aws_region=us-east-1' \
  -var='renderer_role_name=KatchaChronosAutomation' \
  -out=/tmp/katcha-render-staging.tfplan

terraform show /tmp/katcha-render-staging.tfplan
```

Review the plan before applying it. In addition to the private S3 resources, the
plan should show exactly one `aws_iam_role_policy.renderer_staging_access`
attachment to the existing durable renderer role. It must not create an IAM user or
a second renderer role.

The default bucket name is:

```text
katcha-render-staging-<ACCOUNT_ID>-<REGION>
```

## Apply

Only after the plan shows exactly the expected private S3 resources:

```bash
terraform apply /tmp/katcha-render-staging.tfplan
```

Capture the outputs:

```bash
terraform output bucket_name
terraform output staging_prefix
terraform output renderer_role_arn
terraform output renderer_staging_policy_name
terraform output -raw renderer_iam_policy_json
```

The S3 policy is already attached by Terraform to the resolved durable renderer
role. The JSON output remains available for audit/review only; do not manually
duplicate it onto another principal.

Remotion's version-specific control-plane permissions remain separate and are
managed by `scripts/aws_render_authorize.sh` as described in
`docs/AWS_RENDERING.md`.

Do not run `terraform destroy` as an incident-response step.
