# Durable AWS authentication for Chronos

Katcha's local automation host must not depend on an interactive browser login or
long-lived AWS access keys.

For local/WSL automation, the supported durable path is **IAM Roles Anywhere**:

```text
Chronos / WSL
    |
    | X.509 workload certificate
    v
AWS IAM Roles Anywhere
    |
    | short-lived automatically refreshed credentials
    v
KatchaChronosAutomation IAM role
    |
    +-- Remotion Lambda permissions (applied separately after review)
    +-- Katcha render-staging S3 permissions (applied separately after review)
```

The AWS CLI and SDKs consume the temporary credentials through
`credential_process`. No AWS access key, secret key, or session token is stored in
Katcha's `.env`.

The interactive `katcha` profile remains useful only for initial bootstrap,
break-glass administration, and deliberate infrastructure changes.

## Security model

The bootstrap creates:

- a local root CA under `~/.aws/katcha-roles-anywhere/ca`;
- a one-year workload certificate with subject `CN=katcha-chronos`;
- an IAM Roles Anywhere trust anchor for that CA;
- a dedicated `KatchaChronosAutomation` IAM role;
- a Roles Anywhere profile restricted to that role;
- a local AWS profile named `katcha-automation`;
- AWS's official `aws_signing_helper`, pinned to version `1.8.5` and verified
  against AWS-published SHA-256 checksums.

The role trust policy is restricted by:

- the exact trust-anchor ARN;
- the exact expected AWS account ID; and
- the workload certificate subject CN.

The bootstrap intentionally attaches **no resource permissions** to the role.
Authentication and authorization remain separate review steps.

The CA private key is not mounted into Katcha containers. Only the workload
certificate, its private key, and the credential helper are made available to the
renderer, all read-only.

## 1. Inspect the current account

From the repository root:

```bash
cd ~/src/katcha

aws sts get-caller-identity --profile katcha
```

Copy the exact 12-digit account ID from that output, then set it explicitly:

```bash
export KATCHA_AWS_EXPECTED_ACCOUNT_ID=<exact-12-digit-account-id>
export KATCHA_REMOTION_LAMBDA_REGION=us-east-1
export KATCHA_AWS_BOOTSTRAP_PROFILE=katcha
```

Do not derive the expected account ID automatically inside the same command that
performs privileged writes. The explicit value is the fail-closed guard against
operating in the wrong account.

## 2. Review the Roles Anywhere bootstrap

The default invocation is inspect-only:

```bash
bash scripts/aws_roles_anywhere_bootstrap.sh
```

It verifies the active bootstrap identity and prints the resource names and local
paths. It does not create files or AWS resources.

Review that output before continuing.

## 3. Create durable authentication

After the inspect output is correct:

```bash
bash scripts/aws_roles_anywhere_bootstrap.sh --apply
```

The script is deliberately conservative:

- it refuses an unexpected AWS account;
- it refuses partial local certificate state instead of overwriting files;
- it backs up `~/.aws/config` before changing it;
- it reuses complete named resources on rerun;
- it verifies the final durable profile through STS;
- it never creates an AWS access key;
- it never attaches broad Remotion or S3 permissions;
- it persists only non-secret runtime metadata into Katcha's ignored local `.env`.

With the default project layout, the bootstrap writes these values to
`~/src/katcha/.env`:

```env
KATCHA_AWS_EXPECTED_ACCOUNT_ID=<verified-account-id>
KATCHA_REMOTION_LAMBDA_REGION=us-east-1
KATCHA_AWS_PROFILE=katcha-automation
KATCHA_HOST_UID=<host-uid>
KATCHA_HOST_GID=<host-gid>
KATCHA_AWS_SIGNING_HELPER_PATH=/home/<user>/.local/bin/aws_signing_helper
KATCHA_AWS_CERT_PATH=/home/<user>/.aws/katcha-roles-anywhere/runtime/client.pem
KATCHA_AWS_PRIVATE_KEY_PATH=/home/<user>/.aws/katcha-roles-anywhere/runtime/client-key.pem
```

Those entries contain an account guard, profile name, numeric UID/GID, and file
paths. They are not AWS access credentials. The private key itself remains outside
the repository with owner-only permissions. If `.env` already exists, the script
backs it up before changing only these managed keys.

This persistence is what removes the need for per-shell AWS exports during normal
Docker Compose automation.

## 4. Prove that browser login is no longer required

Open a fresh shell. Do not run `aws login`.

Then run:

```bash
aws sts get-caller-identity --profile katcha-automation
```

The ARN should resolve to the dedicated `KatchaChronosAutomation` role and the
account must match `KATCHA_AWS_EXPECTED_ACCOUNT_ID`.

The credential helper exchanges the workload certificate for temporary credentials
when the CLI/SDK needs them. The temporary credentials expire; the certificate-based
credential source remains available without another browser login.

## 5. Use the durable profile inside the renderer

The existing `docker-compose.aws-render.yml` mounts `~/.aws` read-only and makes
the selected AWS profile available to the renderer.

The additional `docker-compose.aws-roles-anywhere.yml` overlay mirrors the
credential helper, workload certificate, and workload private key at their exact
absolute host paths. This is required because `credential_process` in
`~/.aws/config` references those paths directly.

After the bootstrap succeeds, no per-shell exports are needed for the renderer.
Docker Compose reads the persisted non-secret metadata from the project's local
`.env`.

Start or recreate only the renderer:

```bash
docker compose \
  -f docker-compose.yml \
  -f docker-compose.aws-render.yml \
  -f docker-compose.aws-roles-anywhere.yml \
  up -d --no-deps --force-recreate renderer
```

Do not put the certificate private-key **contents** in `.env`, the repository,
a Docker image, or a Compose value. Only its filesystem path is persisted. The key
file itself remains outside the repository, owner-readable only, and is mounted
read-only.

## 6. Authorization is the next gate

A successful Roles Anywhere setup proves only that Katcha can obtain temporary AWS
credentials for the dedicated role.

Before Remotion infrastructure deployment or cloud rendering, separately review and
apply the required permissions:

```bash
cd ~/src/katcha/renderer

npm install
npm run lambda:policy:user > /tmp/remotion-user-policy.json
npm run lambda:policy:role > /tmp/remotion-role-policy.json
```

Review both files before attaching or applying anything.

The Katcha render-staging bucket policy is also generated separately from
`infra/aws-render-staging`. Keep those permissions scoped to the dedicated
automation role.

After authorization is configured, return to
[`AWS_RENDERING.md`](AWS_RENDERING.md) and run the no-write preflight before
creating Remotion resources.

## Rotation

The generated workload certificate is valid for one year. Rotation should happen
before expiration.

The root CA is deliberately longer-lived and its private key is needed only to issue
or rotate workload certificates. For stronger operational security, store the root
CA private key offline after initial setup and bring it back only for controlled
certificate rotation.

Do not delete the active workload certificate or key until the replacement has been
validated with:

```bash
aws sts get-caller-identity --profile katcha-automation
```

## Rollback

To stop using durable authentication for the renderer, switch the local
`KATCHA_AWS_PROFILE` value in `.env` only as part of a deliberate operator
change, or omit the Roles Anywhere Compose overlay. The interactive bootstrap
profile remains available explicitly when needed:

```bash
aws sts get-caller-identity --profile katcha
```

Do not replace the durable profile with static access keys.

Do not delete the IAM role, trust anchor, Roles Anywhere profile, CA, or workload
certificate as an incident-response shortcut. Disable or rotate credentials first,
then perform teardown as a separate reviewed operation.

## References

- AWS IAM Roles Anywhere:
  <https://docs.aws.amazon.com/rolesanywhere/latest/userguide/introduction.html>
- Credential helper:
  <https://docs.aws.amazon.com/rolesanywhere/latest/userguide/credential-helper.html>
- Roles Anywhere trust model:
  <https://docs.aws.amazon.com/rolesanywhere/latest/userguide/trust-model.html>
