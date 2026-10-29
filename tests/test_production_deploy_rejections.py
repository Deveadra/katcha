"""Privileged failure receipts preserve fixed diagnostics without process output."""

import json
from types import SimpleNamespace

import pytest
from test_production_deploy import module, poller

SHA = "a" * 40


def rejected(**diagnostic):
    return json.dumps(
        {
            "status": "BLOCKED",
            "sha": SHA,
            "diagnostic": {"code": "HOST_DISK_FULL", "operation": "docker.pull", **diagnostic},
        }
    )


def test_disk_full_is_classified_without_disclosing_registry_or_path(monkeypatch):
    app = module()
    secret = "https://registry.invalid/private?token=super-secret"
    monkeypatch.setattr(
        app.subprocess,
        "run",
        lambda *a, **kw: SimpleNamespace(
            returncode=1, stdout=secret, stderr="write /private: no space left on device " + secret
        ),
    )
    with pytest.raises(app.Blocked) as failure:
        app.run("docker", "pull", secret)
    diagnostic = app.rejection_diagnostic(failure.value)
    assert diagnostic == {"code": "HOST_DISK_FULL", "operation": "docker.pull"}
    assert "secret" not in json.dumps(diagnostic)
    assert poller().executor_rejection(2, rejected(), SHA)["code"] == "HOST_DISK_FULL"


def test_host_timeout_preserves_only_fixed_operation(monkeypatch):
    app = module()

    def timeout(*a, **kw):
        raise app.subprocess.TimeoutExpired("secret-command", 3, output="private-output")

    monkeypatch.setattr(app.subprocess, "run", timeout)
    with pytest.raises(app.Blocked) as failure:
        app.run("docker", "inspect", "private-image")
    assert app.rejection_diagnostic(failure.value) == {
        "code": "HOST_OPERATION_TIMEOUT",
        "operation": "docker.inspect",
    }
    assert app.rejection_diagnostic(OSError("private-key")) == {
        "code": "EXECUTOR_ERROR",
        "operation": "unknown",
    }


@pytest.mark.parametrize(
    "output",
    [
        "DEPLOY_BLOCKED: secret",
        rejected(code="private-secret"),
        rejected(operation="docker.pull https://private?token=secret"),
        rejected(reason="secret"),
        rejected(code=[]),
        rejected().replace(SHA, "b" * 40),
        json.dumps({"status": "BLOCKED", "sha": SHA, "diagnostic": None}),
        rejected() + "\nsecret",
        " " * 2049,
        "[" * 1001 + "]" * 1001,
    ],
)
def test_rejection_parser_never_saves_unknown_output(output):
    app = poller()
    result = app.executor_rejection(2, output, SHA)
    assert result["code"] == "EXECUTOR_REJECTION_UNCLASSIFIED"
    assert "secret" not in json.dumps(result)
    assert app.executor_rejection(0, output, SHA)["code"] == "EXECUTOR_OUTPUT_UNVERIFIED"


@pytest.mark.parametrize("mode", ["disk", "timeout", "legacy", "success"])
def test_attempt_keeps_consumed_nonce_and_safe_private_receipt(monkeypatch, tmp_path, mode):
    app = poller()
    monkeypatch.setattr(app, "RECEIPTS", tmp_path)
    monkeypatch.setattr(app, "secure_receipts", lambda: None)
    monkeypatch.setattr(
        app,
        "EXECUTOR",
        SimpleNamespace(
            is_symlink=lambda: False,
            is_file=lambda: True,
            stat=lambda: SimpleNamespace(st_uid=0, st_mode=0o755),
        ),
    )
    calls = []

    def execute(*args, **kwargs):
        calls.append(args)
        if mode == "timeout":
            raise app.subprocess.TimeoutExpired("private-command", 7200, output="super-secret")
        stdout = (
            json.dumps({"status": "DEPLOYED", "sha": SHA})
            if mode == "success"
            else "super-secret"
            if mode == "legacy"
            else rejected()
        )
        return SimpleNamespace(
            returncode=0 if mode == "success" else 2,
            stdout=stdout,
            stderr="private signed payload super-secret",
        )

    monkeypatch.setattr(app.subprocess, "run", execute)
    request = {
        "id": 7,
        "nonce": "b" * 32,
        "sha": SHA,
        "policy": "approved-resume",
        "migration_policy": "refuse",
    }
    status = app.attempt(request)
    assert status == ("SUCCEEDED" if mode == "success" else "FAILED_REVIEW_REQUIRED")
    path = tmp_path / ("nonce-" + request["nonce"] + ".json")
    receipt = json.loads(path.read_text())
    assert path.stat().st_mode & 0o777 == 0o600
    assert "secret" not in path.read_text() and "stderr" not in receipt
    if mode == "success":
        assert receipt["diagnostic"] is None
    else:
        assert receipt["executor_status"] is None
        assert (
            receipt["diagnostic"]["code"]
            == {
                "disk": "HOST_DISK_FULL",
                "timeout": "EXECUTOR_TIMEOUT",
                "legacy": "EXECUTOR_REJECTION_UNCLASSIFIED",
            }[mode]
        )
    assert app.attempt(request) == "ALREADY_CONSUMED"
    assert len(calls) == 1
