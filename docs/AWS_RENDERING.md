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

After the policies/role are installed by an authorized AWS administrator:

```bash
npm run lambda:policy:validate
npx remotion lambda regions
npx remotion lambda quotas --region=us-east-1
```

Do not proceed if permission validation fails or the AWS caller identity is not the intended account.

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

## Phase 2: Katcha S3 production storage

Production Lambda rendering cannot use the local MinIO endpoint. Katcha must use a cloud-reachable
Amazon S3 bucket for source media and narration.

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

The renderer validates required Lambda configuration at startup. It also rejects local/private
media URLs before invoking AWS.

## Acceptance order

Do not start with a live YouTube production.

1. Validate IAM and quotas.
2. Deploy the Remotion function and site.
3. Put a small owned fixture into the Katcha AWS S3 media bucket.
4. Run a single synthetic ranked render through the normal Katcha HTTP renderer.
5. Verify the returned metadata says `renderer=remotion-lambda` and `verified=true`.
6. Download and inspect the resulting Katcha S3 object.
7. Recover the existing RankSnaxx render lineage using the cloud backend.
8. Only after visual/audio inspection continue to private YouTube upload.

## Rollback

Cloud rendering is an opt-in switch. To stop using AWS Lambda without altering episode lineage:

```env
KATCHA_RENDER_BACKEND=local
```

Restart only the renderer service. Existing verified render objects remain reusable.

Do not delete Remotion functions, buckets, or Katcha media objects during incident response.
Resource teardown is a separate, explicit operation.
