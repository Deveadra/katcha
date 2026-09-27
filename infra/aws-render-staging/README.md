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

## Review-gated provisioning workflow

Run staging infrastructure from the repository root through
`scripts/aws_render_staging.sh`. The helper keeps the Terraform state bucket,
reviewable plan, plan approval, apply, output verification, and Katcha runtime metadata
in one guarded workflow while still requiring explicit write gates.

If the remote Terraform state bucket does not exist yet, create/repair **only** that
state bucket:

```bash
bash scripts/aws_render_staging.sh --bootstrap-state
```

That mode delegates to the existing account-guarded state bootstrap. It does not run
a render-staging Terraform apply.

Next, generate the staging plan:

```bash
bash scripts/aws_render_staging.sh
```

The default mode:

- verifies the bootstrap/admin caller against the explicit expected account;
- requires the deterministic remote state bucket to already exist;
- initializes Terraform with S3 state and native lockfiles;
- runs `terraform fmt -check` and `terraform validate`;
- writes the binary plan under
  `~/.aws/katcha-roles-anywhere/terraform-plans/`, outside the repository;
- prints the full human-readable plan and its SHA-256;
- performs no Terraform apply.

Review the plan. It should show the private S3 resources and exactly one
`aws_iam_role_policy.renderer_staging_access` attachment to the existing
`KatchaChronosAutomation` role. It must not create an IAM user or a second renderer
role.

Apply only the exact reviewed plan by passing back the printed SHA-256:

```bash
bash scripts/aws_render_staging.sh --apply <reviewed-plan-sha256>
```

The apply mode refuses a missing, changed, or differently hashed plan. After apply it
verifies the bucket, prefix, renderer role ARN, and `KatchaRenderStagingAccess`
policy name. It also verifies that the durable `katcha-automation` profile can read
the bucket location.

Only after those checks pass does the helper persist the non-secret runtime metadata
to the ignored local `.env`:

```env
KATCHA_REMOTION_STAGING_BUCKET=katcha-render-staging-<ACCOUNT_ID>-<REGION>
KATCHA_REMOTION_STAGING_PREFIX=katcha-render-staging
KATCHA_REMOTION_STAGING_URL_EXPIRES_SECONDS=<verified-safe-expiry>
```

The URL expiry is automatically kept at least five minutes beyond Katcha's configured
Lambda wait ceiling. Existing env files are backed up outside the repository before
mutation. `KATCHA_RENDER_BACKEND` is never changed by staging provisioning.

The emitted Terraform policy JSON remains available for audit through
`terraform output -raw renderer_iam_policy_json`; do not manually duplicate it onto
another principal.

Remotion's version-specific control-plane permissions remain separate and are
managed by `scripts/aws_render_authorize.sh` as described in
`docs/AWS_RENDERING.md`.

Do not run `terraform destroy` as an incident-response step.
