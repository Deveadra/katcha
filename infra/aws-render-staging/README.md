# Katcha cloud-render staging bucket

This Terraform root creates only the private S3 staging bucket used by the hybrid
local-MinIO -> AWS Remotion Lambda render path.

It does not deploy Remotion Lambda functions, create IAM users, attach policies, or
modify the Katcha database.

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
- outputs a least-privilege renderer policy document but does not attach it automatically.

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

Initialize this root with partial backend configuration:

```bash
cd ~/src/katcha/infra/aws-render-staging

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
  -out=/tmp/katcha-render-staging.tfplan

terraform show /tmp/katcha-render-staging.tfplan
```

Review the plan before applying it. The default bucket name is:

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
terraform output -raw renderer_iam_policy_json
```

Attach only the emitted renderer S3 policy to the dedicated Katcha renderer principal.
Remotion's own version-specific IAM permissions are generated separately by the
Remotion CLI as described in `docs/AWS_RENDERING.md`.

Do not run `terraform destroy` as an incident-response step.
