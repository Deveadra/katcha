#!/usr/bin/env python3
"""Isolated authorization boundary tests; never contacts AWS."""
import contextlib
import importlib.util
import io
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

MODULE_PATH = Path(__file__).with_name("aws_cloudwatch_diagnostics.py")
spec = importlib.util.spec_from_file_location("katcha_cloudwatch_diagnostics", MODULE_PATH)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
ACCOUNT = "123456789012"
REGION = "us-east-1"
ROLE = "KatchaChronosAutomation"
ENV = {"KATCHA_AWS_EXPECTED_ACCOUNT_ID": ACCOUNT, "KATCHA_REMOTION_LAMBDA_REGION": REGION,
       "KATCHA_AWS_AUTOMATION_ROLE": ROLE, "KATCHA_AWS_BOOTSTRAP_PROFILE": "katcha",
       "KATCHA_AWS_AUTOMATION_PROFILE": "katcha-automation", "KATCHA_ENV_FILE": "/nonexistent/katcha.env"}


class FakeAWS:
    def __init__(self, account=ACCOUNT, runtime_role=ROLE, bad_readback=False, deny=None):
        self.calls = []
        self.account, self.runtime_role = account, runtime_role
        self.bad_readback, self.deny = bad_readback, deny

    def __call__(self, profile, region, service, action, *args, **kwargs):
        self.calls.append((profile, region, service, action, args))
        if self.deny == (service, action):
            raise RuntimeError(f"AccessDenied: {service}:{action}")
        if (service, action) == ("sts", "get-caller-identity"):
            arn = (f"arn:aws:sts::{self.account}:assumed-role/{self.runtime_role}/test"
                   if profile == "katcha-automation" else f"arn:aws:iam::{self.account}:user/bootstrap")
            return {"Account": self.account, "Arn": arn}
        if (service, action) == ("iam", "get-role"):
            return {"Role": {"Arn": f"arn:aws:iam::{ACCOUNT}:role/{ROLE}"}}
        if (service, action) == ("iam", "get-role-policy"):
            return {"PolicyDocument": {"Version": "2012-10-17", "Statement": []}
                    if self.bad_readback else mod.policy(ACCOUNT, REGION)}
        if (service, action) == ("logs", "describe-log-groups"):
            return {"logGroups": [{"logGroupName": "/aws/lambda/remotion-render-test"}]}
        if (service, action) == ("logs", "describe-log-streams"):
            return {"logStreams": [{"logStreamName": "2026/10/16/a"}]}
        return {}


class CloudWatchIAMTests(unittest.TestCase):
    def run_mode(self, cli, fake):
        with patch.dict(os.environ, ENV, clear=True), patch.object(sys, "argv", [str(MODULE_PATH), *cli]), \
             patch.object(mod, "aws", side_effect=fake), contextlib.redirect_stdout(io.StringIO()) as out:
            mod.main()
            return out.getvalue()

    def test_scoped_read_only_policy(self):
        document = mod.policy(ACCOUNT, REGION)
        statements = document["Statement"]
        actions = {x for statement in statements for x in statement["Action"]}
        self.assertEqual(actions, {"logs:DescribeLogGroups", "logs:DescribeLogStreams", "logs:FilterLogEvents",
                                   "logs:GetLogEvents", "cloudwatch:ListMetrics", "cloudwatch:GetMetricData",
                                   "cloudwatch:GetMetricStatistics"})
        self.assertTrue(all(statement["Effect"] == "Allow" for statement in statements))
        for statement in statements:
            if statement["Resource"] == "*":
                self.assertEqual(statement["Condition"], {"StringEquals": {"aws:RequestedRegion": REGION}})
            else:
                self.assertIn(f"arn:aws:logs:{REGION}:{ACCOUNT}:log-group:/aws/lambda/remotion-render-",
                              str(statement["Resource"]))

    def test_default_never_writes_iam(self):
        fake = FakeAWS()
        self.assertIn("INSPECT ONLY", self.run_mode([], fake))
        self.assertNotIn("put-role-policy", [call[3] for call in fake.calls])

    def test_apply_verifies_readback_and_runtime(self):
        fake = FakeAWS()
        self.assertIn("PASS: CloudWatch Logs and Metrics", self.run_mode(["--apply"], fake))
        self.assertEqual(1, sum(call[3] == "put-role-policy" for call in fake.calls))
        self.assertTrue({("logs", "get-log-events"), ("logs", "filter-log-events"),
                         ("cloudwatch", "get-metric-statistics"),
                         ("cloudwatch", "get-metric-data")}.issubset({(c[2], c[3]) for c in fake.calls}))

    def test_mismatched_bootstrap_account_fails_closed(self):
        fake = FakeAWS(account="999999999999")
        with self.assertRaisesRegex(RuntimeError, "bootstrap account mismatch"):
            self.run_mode(["--apply"], fake)
        self.assertNotIn("put-role-policy", [call[3] for call in fake.calls])

    def test_mismatched_runtime_role_fails_closed(self):
        fake = FakeAWS(runtime_role="OtherRole")
        with self.assertRaisesRegex(RuntimeError, "not the intended role"):
            self.run_mode(["--verify"], fake)
        self.assertNotIn("put-role-policy", [call[3] for call in fake.calls])

    def test_denied_reads_fail(self):
        fake = FakeAWS(deny=("logs", "filter-log-events"))
        with self.assertRaisesRegex(RuntimeError, "AccessDenied"):
            self.run_mode(["--verify"], fake)

    def test_apply_retries_only_transient_denial(self):
        fake = FakeAWS()
        state = {"denied": False}

        def briefly_denied(profile, region, service, action, *args, **kwargs):
            if (service, action) == ("logs", "describe-log-groups") and not state["denied"]:
                state["denied"] = True
                raise RuntimeError("AccessDeniedException: no identity-based policy allows DescribeLogGroups")
            return fake(profile, region, service, action, *args, **kwargs)

        with patch.object(mod.time, "sleep") as sleep:
            self.assertIn("PASS: CloudWatch Logs and Metrics", self.run_mode(["--apply"], briefly_denied))
        sleep.assert_called_once_with(2)
        self.assertEqual(1, sum(call[3] == "put-role-policy" for call in fake.calls))

    def test_apply_exhausted_retry_still_fails(self):
        fake = FakeAWS(deny=("logs", "describe-log-groups"))
        with patch.object(mod.time, "sleep") as sleep:
            with self.assertRaisesRegex(RuntimeError, "still denied after bounded IAM propagation retries"):
                self.run_mode(["--apply"], fake)
        self.assertEqual([2, 4, 8, 16], [call.args[0] for call in sleep.call_args_list])
        self.assertEqual(5, sum(call[3] == "describe-log-groups" for call in fake.calls))
        self.assertEqual(1, sum(call[3] == "put-role-policy" for call in fake.calls))

    def test_apply_role_mismatch_never_retries(self):
        fake = FakeAWS(runtime_role="OtherRole")
        with patch.object(mod.time, "sleep") as sleep:
            with self.assertRaisesRegex(RuntimeError, "not the intended role"):
                self.run_mode(["--apply"], fake)
        sleep.assert_not_called()

    def test_apply_non_permission_error_does_not_retry(self):
        fake = FakeAWS()
        def invalid_request(profile, region, service, action, *args, **kwargs):
            if (service, action) == ("logs", "describe-log-groups"):
                raise RuntimeError("ValidationException: invalid log group prefix")
            return fake(profile, region, service, action, *args, **kwargs)
        with patch.object(mod.time, "sleep") as sleep:
            with self.assertRaisesRegex(RuntimeError, "ValidationException"):
                self.run_mode(["--apply"], invalid_request)
        sleep.assert_not_called()

    def test_readback_mismatch_fails(self):
        fake = FakeAWS(bad_readback=True)
        with self.assertRaisesRegex(RuntimeError, "IAM readback differs"):
            self.run_mode(["--apply"], fake)


if __name__ == "__main__":
    unittest.main()
