# AWS cloud rendering

Katcha supports two render backends:

- `local`: the existing Docker/Remotion renderer. This remains the default for development and previews.
- `lambda`: Remotion Lambda on AWS for production video renders.

The Python/Temporal production contract does not change. The renderer HTTP service still
accepts the same manifest and returns the same verified Katcha output key.

## Safety rules

1. Do not commit AWS access keys, secret keys, session tokens, or presigned URLs.
2. Prefer IAM roles, AWS IAM Identity Center / SSO, or another AWS SDK credential-chain source.
3. Keep Katcha media storage separate from Remotion's `remotionlambda-*` deployment/render bucket.
4. Keep S3 Block Public Access enabled for the Katcha media bucket.
5. Katcha signs source media for time-limited HTTPS reads. Lambda is never given direct access to
   local MinIO, `localhost`, `acceptance-media`, Docker-only hostnames, or private RFC1918 addresses.
6. A Lambda render is not accepted merely because Remotion reports completion. Katcha downloads the
   artifact, verifies duration and dimensions with ffprobe, uploads it to the configured Katcha
   object store, HEAD-verifies the stored object, and only then returns `verified=true`.
7. Do not switch `KATCHA_RENDER_BACKEND=lambda` until the preflight steps below all pass.

## Version contract

All Remotion packages are pinned to `4.0.529`. Remotion requires all `remotion` and
`@remotion/*` packages to use the same exact version for Lambda deployments.

Do not independently bump one Remotion package.

## Phase 0: no-write preflight

From `~/src/katcha/renderer`:

```bash
npm install
npm run test:config

aws sts get-caller-identity

npm run lambda:policy:user > /tmp/remotion-user-policy.json
npm run lambda:policy:role > /tmp/remotion-role-policy.json
```

Review both generated policy files before applying them. The generated policies are version-specific.

After the policies/role are installed by an authorized AWS administrator, run the
fail-closed preflight from the repository root. The expected AWS account ID is mandatory:

```bash
export KATCHA_AWS_EXPECTED_ACCOUNT_ID=123456789012
export KATCHA_REMOTION_LAMBDA_REGION=us-east-1

bash scripts/aws_render_preflight.sh
```

The script verifies the active AWS caller against the expected account, validates the
local renderer configuration, validates Remotion permissions, and reads the regional
Lambda quota. It contains no resource-creation command.

Do not proceed if any preflight check fails.

## Phase 0.5: durable Terraform state

Before provisioning any Terraform-managed AWS render resources, bootstrap remote S3 state.
The bootstrap script is inspect-only unless `--apply` is supplied:

```bash
export KATCHA_AWS_EXPECTED_ACCOUNT_ID=123456789012
export KATCHA_REMOTION_LAMBDA_REGION=us-east-1

bash scripts/aws_tf_state_bootstrap.sh
# Review the output.
bash scripts/aws_tf_state_bootstrap.sh --apply
```

The state bucket is separate from both Katcha media and Remotion render buckets. It has
versioning enabled and Terraform uses the S3 backend's native lockfile support.

## Phase 1: Remotion infrastructure

This section creates AWS resources and must be run intentionally.

From `~/src/katcha/renderer`:

```bash
npx remotion lambda functions deploy \
  --region=us-east-1 \
  --memory=4096 \
  --disk=4096 \
  --timeout=900 \
  --retention-period=14
```

Record the returned function name exactly.

Deploy the Katcha Remotion site:

```bash
npx remotion lambda sites create src/entry.jsx \
  --region=us-east-1 \
  --site-name=katcha-production
```

Record the returned HTTPS Serve URL exactly.

A Remotion function is version-specific; one function per region/version is sufficient. Redeploy
the site whenever the Remotion composition code changes.

## Phase 2A: hybrid local storage with private AWS staging

For the current workstation workflow, Katcha can keep MinIO as its canonical object store.
The renderer stages only the exact video/audio/image objects needed by a Lambda render into
a private S3 bucket, signs those temporary objects, and leaves lifecycle expiration to S3.

Provision the bucket from the review-first Terraform root after remote state is initialized
as described in `infra/aws-render-staging/README.md`:

```bash
cd ~/src/katcha/infra/aws-render-staging
terraform plan \
  -var='expected_account_id=123456789012' \
  -var='aws_region=us-east-1' \
  -out=/tmp/katcha-render-staging.tfplan
terraform show /tmp/katcha-render-staging.tfplan
```

Apply only after the plan is reviewed. Then set:

```env
KATCHA_REMOTION_STAGING_BUCKET=katcha-render-staging-123456789012-us-east-1
KATCHA_REMOTION_STAGING_PREFIX=katcha-render-staging
KATCHA_REMOTION_STAGING_URL_EXPIRES_SECONDS=3600
```

The staging URL expiry must exceed Katcha's Lambda wait ceiling by at least five minutes.
Staged objects are private, encrypted, and expire automatically. Katcha never makes the
bucket public.

For a local AWS SSO/profile session, start only the renderer with the explicit overlay:

```bash
export AWS_PROFILE=<dedicated-katcha-profile>
export KATCHA_HOST_UID="$(id -u)"
export KATCHA_HOST_GID="$(id -g)"

docker compose \
  -f docker-compose.yml \
  -f docker-compose.aws-render.yml \
  up -d --no-deps --force-recreate renderer
```

The overlay mounts `$HOME/.aws` read-only and runs the renderer with your host UID/GID so owner-only AWS profile and SSO cache files remain readable without running the container as root. It sets both `AWS_PROFILE` and `REMOTION_AWS_PROFILE`. Refresh SSO on the host before starting the renderer. Do not copy static AWS keys into `.env`.

## Phase 2B: hosted production S3

For a fully hosted deployment, Katcha can instead use Amazon S3 as its canonical object
store. In that mode staging is unnecessary because the renderer can presign the canonical
objects directly.

Production settings:

```env
KATCHA_S3_ENDPOINT_URL=
KATCHA_S3_REGION=us-east-1
KATCHA_S3_FORCE_PATH_STYLE=false
KATCHA_S3_ACCESS_KEY=
KATCHA_S3_SECRET_KEY=
```

Blank explicit keys mean the AWS SDK credential chain is used. In hosted production, attach an IAM
role to the workload rather than injecting long-lived user credentials.

Do not reuse Remotion's `remotionlambda-*` bucket as Katcha's canonical media store.

## Phase 3: enable cloud rendering

Only after the function, site, Katcha S3 storage, and IAM path are verified:

```env
KATCHA_RENDER_BACKEND=lambda
KATCHA_REMOTION_LAMBDA_REGION=us-east-1
KATCHA_REMOTION_LAMBDA_FUNCTION_NAME=<exact deployed function name>
KATCHA_REMOTION_LAMBDA_SERVE_URL=<exact HTTPS serve URL>
KATCHA_REMOTION_LAMBDA_POLL_INTERVAL_MS=2000
KATCHA_REMOTION_LAMBDA_MAX_WAIT_MS=1500000
# 25-minute orchestration ceiling; stays below Katcha's 30-minute renderer HTTP timeout.
KATCHA_REMOTION_LAMBDA_MAX_RETRIES=2
KATCHA_REMOTION_LAMBDA_FRAMES_PER_LAMBDA=20
KATCHA_REMOTION_LAMBDA_CONCURRENCY_PER_LAMBDA=1
```

The renderer validates required Lambda configuration at startup. Without a staging bucket it
rejects local/private media URLs before invoking AWS. With staging enabled, local MinIO objects
are copied into private AWS S3 objects first and only the temporary HTTPS presigned URLs are
sent to Remotion Lambda.

## Acceptance order

Do not start with a live YouTube production.

1. Validate IAM and quotas.
2. Deploy the Remotion function and site.
3. Provision/review the private staging bucket, or configure canonical AWS S3.
4. Stage a small owned fixture through the normal Katcha renderer.
5. Run a single synthetic ranked render through the normal Katcha HTTP renderer.
6. Verify the returned metadata says `renderer=remotion-lambda` and `verified=true`.
7. Download and inspect the resulting Katcha object.
8. Recover the existing RankSnaxx render lineage using the cloud backend.
9. Only after visual/audio inspection continue to private YouTube upload.

## Rollback

Cloud rendering is an opt-in switch. To stop using AWS Lambda without altering episode lineage:

```env
KATCHA_RENDER_BACKEND=local
```

Restart only the renderer service. Existing verified render objects remain reusable.

Do not delete Remotion functions, buckets, or Katcha media objects during incident response.
Resource teardown is a separate, explicit operation.
