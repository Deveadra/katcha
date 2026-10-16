# AWS CloudWatch diagnostics

Katcha uses IAM Roles Anywhere to assume the durable `KatchaChronosAutomation` role
through the `katcha-automation` AWS CLI profile. The separate Remotion control-plane
policy and S3 render-staging policy do not automatically grant CloudWatch read access.

## Exact access granted

The review-gated helper attaches only the dedicated inline policy
`KatchaCloudWatchReadDiagnostics` to the existing automation role.

- `logs:DescribeLogGroups` on `Resource: "*"` (AWS requires discovery scope),
  restricted to the intended region.
- `logs:DescribeLogStreams`, `logs:FilterLogEvents`, and `logs:GetLogEvents`
  limited to `/aws/lambda/remotion-render-*` log groups and their streams
  in the verified account and region.
- `cloudwatch:ListMetrics`, `cloudwatch:GetMetricData`, and
  `cloudwatch:GetMetricStatistics` with `Resource: "*"` because classic metrics
  calls cannot be restricted to individual Lambda functions through ordinary
  IAM resource ARNs; the region is restricted.
- No log writing, editing, deletion, `logs:Unmask`, Logs Insights query
  execution, or additional IAM permissions on the runtime role.

These grants do not change the Remotion execution role, renderer backend,
Roles Anywhere trust, staging bucket, or production poller. Read access can
still be denied by permissions boundaries, session policies, or AWS SCPs.

## Inspect, apply, verify

Run from your Katcha checkout after reauthenticating the `katcha` bootstrap
profile. The script reads the expected account and region from environment
variables or the ignored local `.env`; it never requests or prints credentials.

```bash
cd ~/src/katcha

# Print verified account, target role ARN, policy JSON and SHA-256. No IAM writes.
python3 scripts/aws_cloudwatch_diagnostics.py

# Only after reviewing the policy and account:
python3 scripts/aws_cloudwatch_diagnostics.py --apply

# No IAM writes; uses the actual katcha-automation role to check read access.
python3 scripts/aws_cloudwatch_diagnostics.py --verify
```

The apply step writes exactly one named inline IAM policy, reads it back and
checks the durable role using bounded CloudWatch Logs and metrics queries.
IAM propagation can take time. If the policy readback passes but a runtime
query returns `AccessDenied`, re-run **only** `--verify` before assuming more
permissions are required.

If no log group exists yet, successful discovery is not evidence that Lambda
has emitted logs. After a Remotion invocation, repeat `--verify` so the
log-stream and event-read actions can be tested. Metrics requests may also
succeed with empty datapoints; this verifies read permissions, not that renders
ran or telemetry was generated.

For an AccessDenied investigation, verify the exact caller ARN, region, role
inline policy, permissions boundary, applicable SCPs and session policies.
Do not attach `CloudWatchFullAccess` or `CloudWatchLogsFullAccess` to work
around the failure. Do not add logs write permissions to the operator role:
if Lambda cannot **emit** logs, inspect the separate pinned Remotion Lambda
execution role policy.

## No-cloud tests

```bash
python3 scripts/test_aws_cloudwatch_diagnostics.py
```

Mock tests verify that policy actions remain read-only and scoped, inspection
cannot mutate IAM, unauthorized accounts and roles fail closed, denied read
calls do not report success, and post-apply IAM readback is enforced.

**Operational boundary:** Merging this change does not modify AWS IAM.
Applying the reviewed policy requires the existing authorized AWS bootstrap
profile on the operator's machine. Do not claim CloudWatch access restored
until a real `--verify` succeeds.