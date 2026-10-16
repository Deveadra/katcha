#!/usr/bin/env python3
"""Review-gated, read-only CloudWatch diagnostics for Katcha's AWS automation role."""
import argparse
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
POLICY_NAME = "KatchaCloudWatchReadDiagnostics"


def configured(key: str, default: str = "") -> str:
    if os.getenv(key):
        return os.environ[key]
    path = Path(os.getenv("KATCHA_ENV_FILE", str(ROOT / ".env")))
    if not path.is_absolute():
        path = ROOT / path
    if path.is_file():
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            name, value = line.split("=", 1)
            if name.strip() == key:
                return value.strip().strip("\"'")
    return default


def aws(profile: str, region: str, *args: str, as_json: bool = False):
    cmd = ["aws", *args, "--profile", profile, "--region", region, "--output", "json" if as_json else "text"]
    try:
        p = subprocess.run(cmd, check=True, text=True, capture_output=True)
    except (OSError, subprocess.CalledProcessError) as e:
        detail = (e.stderr or "").strip() if isinstance(e, subprocess.CalledProcessError) else str(e)
        raise RuntimeError(f"AWS {args[0]}:{args[1]} failed ({profile}, {region}): {detail}") from e
    return json.loads(p.stdout) if as_json else p.stdout.strip()


def policy(account: str, region: str) -> dict:
    group = f"arn:aws:logs:{region}:{account}:log-group:/aws/lambda/remotion-render-*"
    return {"Version": "2012-10-17", "Statement": [
        {"Sid": "DiscoverGroups", "Effect": "Allow", "Action": ["logs:DescribeLogGroups"],
         "Resource": "*", "Condition": {"StringEquals": {"aws:RequestedRegion": region}}},
        {"Sid": "ReadGroups", "Effect": "Allow", "Action": ["logs:DescribeLogStreams", "logs:FilterLogEvents"],
         "Resource": [group, group + ":*"]},
        {"Sid": "ReadStreams", "Effect": "Allow", "Action": ["logs:GetLogEvents"],
         "Resource": group + ":log-stream:*"},
        {"Sid": "ReadMetrics", "Effect": "Allow", "Action": ["cloudwatch:ListMetrics", "cloudwatch:GetMetricData", "cloudwatch:GetMetricStatistics"],
         "Resource": "*", "Condition": {"StringEquals": {"aws:RequestedRegion": region}}},
    ]}


def verify(account: str, region: str, role: str, profile: str, function: str):
    identity = aws(profile, region, "sts", "get-caller-identity", as_json=True)
    arn = identity.get("Arn", "")
    expected = f"arn:aws:sts::{account}:assumed-role/{role}/"
    if identity.get("Account") != account or not arn.startswith(expected):
        raise RuntimeError(f"BLOCKED: runtime AWS caller is not the intended role: {arn}")
    groups = aws(profile, region, "logs", "describe-log-groups", "--log-group-name-prefix",
                 "/aws/lambda/remotion-render-", "--max-items", "10", as_json=True)
    aws(profile, region, "cloudwatch", "list-metrics", "--namespace", "AWS/Lambda", "--max-items", "1", as_json=True)
    stop = datetime.now(timezone.utc)
    start = stop - timedelta(minutes=10)
    iso = lambda dt: dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    aws(profile, region, "cloudwatch", "get-metric-statistics", "--namespace", "AWS/Lambda",
        "--metric-name", "Errors", "--dimensions", f"Name=FunctionName,Value={function or 'remotion-render-not-deployed'}",
        "--statistics", "Sum", "--period", "300", "--start-time", iso(start), "--end-time", iso(stop), as_json=True)
    query = json.dumps([{"Id": "errors", "MetricStat": {"Metric": {"Namespace": "AWS/Lambda",
        "MetricName": "Errors", "Dimensions": [{"Name": "FunctionName",
        "Value": function or "remotion-render-not-deployed"}]}, "Period": 300, "Stat": "Sum"},
        "ReturnData": True}])
    aws(profile, region, "cloudwatch", "get-metric-data", "--metric-data-queries", query,
        "--start-time", iso(start), "--end-time", iso(stop), as_json=True)
    names = [x.get("logGroupName", "") for x in groups.get("logGroups", [])]
    names = [name for name in names if name.startswith("/aws/lambda/remotion-render-")]
    if not names:
        print("PASS: log group discovery and metric reads authorized; no Remotion Lambda log group exists yet.")
        return
    group = names[0]
    streams = aws(profile, region, "logs", "describe-log-streams", "--log-group-name", group,
                  "--limit", "1", as_json=True)
    aws(profile, region, "logs", "filter-log-events", "--log-group-name", group,
        "--limit", "1", as_json=True)
    existing = [x.get("logStreamName", "") for x in streams.get("logStreams", [])]
    if existing and existing[0]:
        aws(profile, region, "logs", "get-log-events", "--log-group-name", group,
            "--log-stream-name", existing[0], "--limit", "1", as_json=True)
    else:
        print("NOTICE: no log streams; GetLogEvents cannot be exercised yet.")
    print(f"PASS: CloudWatch Logs and Metrics read checks for {group}.")



def verify_after_apply(account: str, region: str, role: str, profile: str, function: str):
    """Allow a bounded IAM propagation window without masking permanent denies."""
    retry_delays = (2, 4, 8, 16)  # At most 30 seconds, five read attempts.
    for attempt in range(len(retry_delays) + 1):
        try:
            verify(account, region, role, profile, function)
            return
        except RuntimeError as exc:
            error = str(exc).lower()
            # Caller/role mismatch and unrelated API failures must fail immediately.
            if "accessdenied" not in error and "not authorized" not in error:
                raise
            if attempt == len(retry_delays):
                raise RuntimeError(
                    "BLOCKED: CloudWatch runtime read access still denied after "
                    "bounded IAM propagation retries (30s). "
                    "The policy remains attached; check the underlying AccessDenied "
                    f"and the role's boundaries/SCPs/session policies. Last error: {exc}"
                ) from exc
            delay = retry_delays[attempt]
            print(
                f"NOTICE: CloudWatch read denied after IAM update; "
                f"propagation retry {attempt + 1}/{len(retry_delays)} in {delay}s.",
                file=sys.stderr,
            )
            time.sleep(delay)



def main():
    ap = argparse.ArgumentParser(description=__doc__)
    modes = ap.add_mutually_exclusive_group()
    modes.add_argument("--apply", action="store_true", help="Attach the reviewed policy, then verify")
    modes.add_argument("--verify", action="store_true", help="Only test read permissions as runtime role")
    args = ap.parse_args()
    account = configured("KATCHA_AWS_EXPECTED_ACCOUNT_ID")
    region = configured("KATCHA_REMOTION_LAMBDA_REGION", "us-east-1")
    role = configured("KATCHA_AWS_AUTOMATION_ROLE", "KatchaChronosAutomation")
    admin = configured("KATCHA_AWS_BOOTSTRAP_PROFILE", "katcha-admin")
    runtime = configured("KATCHA_AWS_AUTOMATION_PROFILE", "katcha-automation")
    function = configured("KATCHA_REMOTION_LAMBDA_FUNCTION_NAME")
    if not re.fullmatch(r"\d{12}", account):
        raise RuntimeError("BLOCKED: expected explicit 12-digit KATCHA_AWS_EXPECTED_ACCOUNT_ID")
    if not re.fullmatch(r"[a-z]{2}(?:-gov)?-[a-z0-9-]+-\d+", region):
        raise RuntimeError("BLOCKED: invalid AWS region")
    if not re.fullmatch(r"[A-Za-z0-9+=,.@_-]{1,64}", role):
        raise RuntimeError("BLOCKED: invalid IAM role name")
    if args.verify:
        verify(account, region, role, runtime, function)
        return
    identity = aws(admin, region, "sts", "get-caller-identity", as_json=True)
    if identity.get("Account") != account:
        raise RuntimeError(f"BLOCKED: bootstrap account mismatch: {identity.get('Arn')}")
    bootstrap_arn = identity.get("Arn", "")
    if not re.fullmatch(r"arn:[a-z0-9-]+:(?:iam|sts)::[0-9]{12}:.+", bootstrap_arn):
        raise RuntimeError("BLOCKED: unrecognized AWS bootstrap caller ARN")
    if re.fullmatch(r"arn:[a-z0-9-]+:iam::[0-9]{12}:root", bootstrap_arn):
        raise RuntimeError(
            "BLOCKED: AWS root credentials are not permitted for Katcha administration. "
            "Use an MFA-protected administrator session with temporary credentials, "
            "set KATCHA_AWS_BOOTSTRAP_PROFILE, and consult "
            "docs/AWS_BOOTSTRAP_IDENTITY_MIGRATION.md"
        )
    target = aws(admin, region, "iam", "get-role", "--role-name", role, as_json=True)["Role"]["Arn"]
    if not (target == f"arn:aws:iam::{account}:role/{role}" or
            target.startswith(f"arn:aws:iam::{account}:role/") and target.endswith("/" + role)):
        raise RuntimeError(f"BLOCKED: unexpected target role ARN: {target}")
    document = policy(account, region)
    serialized = json.dumps(document, sort_keys=True, separators=(",", ":"))
    print(f"Account: {account}\nRegion: {region}\nBootstrap caller: {identity['Arn']}\nTarget: {target}")
    print(f"Policy: {POLICY_NAME}\nSHA-256: {sha256((serialized + chr(10)).encode()).hexdigest()}")
    print(json.dumps(document, indent=2, sort_keys=True))
    if not args.apply:
        print("INSPECT ONLY: no IAM writes. Review before using --apply.")
        return
    with tempfile.TemporaryDirectory() as tmp:
        file = Path(tmp) / "policy.json"
        file.write_text(serialized + "\n", encoding="utf-8")
        aws(admin, region, "iam", "put-role-policy", "--role-name", role,
            "--policy-name", POLICY_NAME, "--policy-document", "file://" + str(file))
    got = aws(admin, region, "iam", "get-role-policy", "--role-name", role,
              "--policy-name", POLICY_NAME, as_json=True)
    if got.get("PolicyDocument") != document:
        raise RuntimeError("BLOCKED: IAM readback differs from reviewed policy")
    print("PASS: IAM role policy attached and read back; verifying runtime reads...")
    verify_after_apply(account, region, role, runtime, function)


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, KeyError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
