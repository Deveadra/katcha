"""Operator GitHub deployment preflight, isolated from network and GitHub CLI."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/check-production-deployment-github.sh"

FAKE_GH = """#!/usr/bin/env python3
import os
import sys

args = sys.argv[1:]
if args[:2] == ["auth", "status"]:
    sys.exit(1 if os.environ.get("FAKE_UNAUTHENTICATED") == "1" else 0)
if args[:2] == ["variable", "get"]:
    if os.environ.get("FAKE_GET_ERROR") == "1":
        sys.exit(1)
    print(os.environ.get("FAKE_GATE", "true"))
elif args[:2] == ["variable", "list"]:
    if os.environ.get("FAKE_LIST_ERROR") == "1":
        sys.exit(1)
    if os.environ.get("FAKE_DUPLICATE") == "1":
        print("KATCHA_PRODUCTION_DEPLOY_ENABLED")
elif args[:2] == ["secret", "list"]:
    if os.environ.get("FAKE_SECRET_ERROR") == "1":
        sys.exit(1)
    if os.environ.get("FAKE_SECRET_PRESENT", "1") == "1":
        print("KATCHA_PRODUCTION_DEPLOY_HMAC_KEY")
else:
    sys.exit(12)
"""


def check(tmp_path: Path, **overrides: str) -> subprocess.CompletedProcess[str]:
    stub = tmp_path / "gh"
    stub.write_text(FAKE_GH, encoding="utf-8")
    stub.chmod(0o755)
    env = {
        **os.environ,
        "PATH": str(tmp_path) + os.pathsep + os.environ["PATH"],
        **overrides,
    }
    return subprocess.run(
        ["bash", str(SCRIPT)],
        env=env,
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


def test_preflight_accepts_only_nonduplicated_enabled_gate_and_secret(tmp_path):
    result = check(tmp_path)
    assert result.returncode == 0, result.stderr
    assert "GITHUB_DEPLOY_AUTH_SCOPE_OK" in result.stdout
    assert "repo_gate=true" in result.stdout
    assert "environment_signing_secret=present" in result.stdout


@pytest.mark.parametrize(
    ("settings", "reason"),
    [
        ({"FAKE_GATE": "false"}, "must remain true"),
        ({"FAKE_GET_ERROR": "1"}, "absent or inaccessible"),
        ({"FAKE_DUPLICATE": "1"}, "Conflicting katcha-production"),
        ({"FAKE_LIST_ERROR": "1"}, "Could not inspect GitHub Environment variables"),
        ({"FAKE_SECRET_PRESENT": "0"}, "Missing Environment secret"),
        ({"FAKE_SECRET_ERROR": "1"}, "Could not inspect protected Environment"),
        ({"FAKE_UNAUTHENTICATED": "1"}, "Authenticate GitHub CLI"),
    ],
)
def test_preflight_fails_closed_with_actionable_scope_reason(
    tmp_path, settings, reason,
):
    result = check(tmp_path, **settings)
    assert result.returncode != 0
    assert reason in result.stderr
    assert "GITHUB_DEPLOY_AUTH_SCOPE_OK" not in result.stdout


def test_github_workflow_reports_gate_and_secret_failures_separately():
    workflow = (ROOT / ".github/workflows/production-deployment.yml").read_text()
    assert "GITHUB_DEPLOY_GATE_DISABLED" in workflow
    assert "GITHUB_DEPLOY_SIGNING_SECRET_INVALID" in workflow
    assert "Production deployment request authorization missing or invalid" not in workflow
    assert "core.info(key)" not in workflow
    assert "console.log(key)" not in workflow
    assert "environment: katcha-production" in workflow
