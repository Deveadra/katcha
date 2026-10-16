# Replace AWS account-root bootstrap access

**Incident observed 2026-10-16:** the local bootstrap profile used in Katcha
AWS diagnostics resolved to `arn:aws:iam::199264265875:root`. The existing
`katcha-automation` workload assumes `KatchaChronosAutomation` through IAM
Roles Anywhere and must remain independent. CloudWatch permissions already
work as that non-root role; do not re-create it or change its trust.

Katcha's AWS scripts now reject the actual STS caller when it is AWS root,
regardless of the **profile name**, even in inspection modes that can contact
AWS. The bootstrap default is now `katcha-admin` rather than `katcha`.
No bypass flag is provided. Changing an environment variable or renaming a
root-backed AWS CLI profile will **not** bypass the account identity check.

## 1. Establish a separate MFA-backed administrator (AWS console)

Use the AWS Console, not a root AWS CLI access key, to perform this migration.
Use the existing non-root administrative identity if one already exists.

1. Inspect whether AWS IAM Identity Center with **organization-level AWS
   account access** is configured. If not, activating it may require AWS
   Organizations setup; review its account-wide implications before enabling.
   Do not create an organization implicitly from Katcha scripts.
2. In IAM Identity Center, configure a human operator with phishing-resistant
   MFA where available, and assign AWS account `199264265875` a dedicated
   `KatchaBootstrapAdmin` permission set. Grant the IAM, Roles Anywhere,
   Lambda, S3, and Terraform administration permissions actually required by
   the reviewed operations. Where initial administration needs broader
   permissions, make that elevation temporary, review it, and scope it down
   after the migration. The account-root user is not the steady-state operator.
3. If IAM Identity Center is not an appropriate option, use an explicitly
   reviewed IAM role with temporary STS credentials and MFA-protected human
   authentication. Do not create a replacement long-lived access key merely
   to avoid the root error.
4. Confirm the root console sign-in already has MFA (prefer two registered
   phishing-resistant security keys), strong account recovery controls, and
   a secure root credential recovery process. Do not share secrets in tickets
   or GitHub issues.

AWS guidance:
- [Root best practices](https://docs.aws.amazon.com/IAM/latest/UserGuide/root-user-best-practices.html)
- [IAM Identity Center quick start](https://docs.aws.amazon.com/singlesignon/latest/userguide/awsapps-identity-center-quick-start.html)
- [AWS CLI SSO profile setup](https://docs.aws.amazon.com/cli/latest/userguide/cli-configure-sso.html)

## 2. Create a separate named AWS CLI profile on Chronos/WSL

From `~/src/katcha`, after the operator has been assigned access in the console:

```bash
cd ~/src/katcha

# Guided wizard: enter the real AWS access portal URL and SSO region,
# select account 199264265875 and the approved permission set.
aws configure sso --profile katcha-admin
aws sso login --profile katcha-admin

# Output is safe to share after reviewing account/role ARN; no secrets.
aws sts get-caller-identity --profile katcha-admin \
  --query '{Account:Account,Arn:Arn}' --output json

# Verify Katcha's dedicated existing role can be inspected by the admin.
aws iam get-role --profile katcha-admin --role-name KatchaChronosAutomation \
  --query 'Role.Arn' --output text
```

The output must identify account `199264265875` and a **non-root** caller,
usually `arn:aws:sts::199264265875:assumed-role/AWSReservedSSO_.../... `.
If it prints `arn:aws:iam::199264265875:root`, stop; do not use the profile.

Use this bootstrap profile for interactive, reviewed administration only:

```bash
export KATCHA_AWS_BOOTSTRAP_PROFILE=katcha-admin

# Inspect a real configured AWS helper; no AWS writes.
python3 scripts/aws_cloudwatch_diagnostics.py

# Separately ensure the unattended runtime role remains functional.
python3 scripts/aws_cloudwatch_diagnostics.py --verify
```

Ordinary Katcha workloads keep using `katcha-automation` via IAM Roles
Anywhere, without human SSO logins. **Do not change `KATCHA_AWS_PROFILE` or
the roles-anywhere certificate material to this SSO profile.** When the
human SSO token expires, use `aws sso login --profile katcha-admin` again.
The bootstrap admin session is for intentional changes, not unattended jobs.

## 3. Retire root programmatic access *only after* proof

Do not deactivate any root keys or delete local credential files until the
new profile is confirmed working, its MFA/recovery path is tested, and an
inventory identifies consumers of the old root-backed `katcha` profile.

1. Confirm AWS account ID, non-root ARN, successful IAM read permissions, and
   the `katcha-automation` workload's independent STS and CloudWatch checks.
   Confirm the authorized administrator can make a reviewed IAM change when
   such a change is actually needed. Don't make a dummy IAM change.
2. Inspect the **root user's access keys** in AWS Console -> account menu ->
   Security credentials -> Root user access keys, and review AWS CloudTrail
   for recent root API activity. Never paste/print key IDs or secret material.
3. If root API keys exist, identify any remaining callers; **deactivate** the
   root keys via the console after the replacement is proven. Monitor for
   failures before **deleting** them using AWS's documented root security flow.
   Eliminate stale local root-backed profiles and external copies without
   modifying `katcha-automation` credentials. Record deletion evidence
   without disclosing sensitive identifiers.
4. Preserve the root account's protected break-glass console access and
   recovery methods. Root MFA and root key deletion are account actions;
   a GitHub PR cannot perform or verify them. Never delete all emergency
   recovery access while transitioning.

## 4. Acceptance evidence

- Normal Katcha scripts reject STS ARN `...:root` before IAM, Terraform
  application, rendering, or state-bucket changes.
- `katcha-admin` STS identity is non-root and correct account; its required
  **read-only** preflight passes.
- Existing `katcha-automation` identity and CloudWatch diagnostics still pass.
- Root has MFA; unused root programmatic access is disabled and then deleted,
  **after** the new operator access is verified.

To test the repository guard without accessing AWS:

```bash
bash scripts/test_aws_nonroot_caller_guard.sh
python3 scripts/test_aws_cloudwatch_diagnostics.py
```

An IAM session managed through AWS IAM Identity Center is safer than relying
on persistent root access keys. These changes do **not** create IAM users,
access keys, permission sets, or AWS Organizations resources automatically.
