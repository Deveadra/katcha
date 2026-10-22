"""Offline contract tests for Telegram deployment approval and OCI receipts."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
SHA = "a" * 40
OTHER = "b" * 40


def load(name: str, path: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def notify():
    return load("prod_notify", "deploy/scripts/production-deploy-notify.py")


def poller():
    return load("prod_poll", "deploy/scripts/production-deploy-poll.py")


@pytest.mark.parametrize(
    ("code", "response", "expected"),
    [
        (0, {"status": "DEPLOYED", "sha": SHA}, "DEPLOYED"),
        (0, {"status": "ALREADY_CURRENT", "sha": SHA}, "ALREADY_CURRENT"),
        (0, {"status": "DEPLOYED", "sha": OTHER}, None),
        (0, {"status": "BLOCKED", "sha": SHA}, None),
        (1, {"status": "DEPLOYED", "sha": SHA}, None),
    ],
)
def test_executor_attestation_rejects_exit_only_success(code, response, expected):
    app = poller()
    assert app.executor_attestation(code, json.dumps(response), SHA) == expected
    assert app.executor_attestation(0, "not-json", SHA) is None
    assert app.executor_attestation(0, "[]", SHA) is None


def test_success_notification_requires_exact_live_sha_and_audit():
    app = notify()
    receipt = {"id": 71, "sha": SHA, "status": "SUCCEEDED",
               "executor_status": "DEPLOYED"}
    success = app.receipt_event(receipt, current_sha=SHA, audited=True, age=700)
    assert success and success[0] == "verified"
    assert "VERIFIED SUCCESSFUL" in success[1]
    for live, audited in [(OTHER, True), (None, True), (SHA, False)]:
        outcome = app.receipt_event(
            receipt, current_sha=live, audited=audited, age=700
        )
        assert outcome and outcome[0] == "unverified"
        assert "VERIFIED SUCCESSFUL" not in outcome[1]
    assert app.receipt_event(
        receipt, current_sha=OTHER, audited=False, age=100
    ) is None


def test_already_current_must_not_claim_new_deployment():
    app = notify()
    result = app.receipt_event(
        {"id": 3, "sha": SHA, "status": "SUCCEEDED",
         "executor_status": "ALREADY_CURRENT"},
        current_sha=SHA, audited=False, age=600,
    )
    assert result and result[0] == "already-current"
    assert "no new cutover" in result[1]


def test_failure_and_stall_are_never_success():
    app = notify()
    failed = {"id": 1, "sha": SHA, "status": "FAILED_REVIEW_REQUIRED"}
    assert app.receipt_event(
        failed, current_sha=SHA, audited=True, age=50
    )[0] == "failed"
    started = {"id": 1, "sha": SHA, "status": "STARTED"}
    assert app.receipt_event(
        started, current_sha=SHA, audited=True, age=30
    ) is None
    assert app.receipt_event(
        started, current_sha=SHA, audited=True, age=8200
    )[0] == "stalled"
    assert app.receipt_event(
        {"id": "not-an-id", "sha": SHA, "status": "SUCCEEDED"},
        current_sha=SHA, audited=True, age=600,
    ) is None


def test_root_private_telegram_config_is_parsed_without_shell(monkeypatch, tmp_path):
    app = notify()
    config = tmp_path / "config.env"
    config.write_text(
        "KATCHA_TELEGRAM_BOT_TOKEN='123:secret-Bot_token'\n"
        "KATCHA_TELEGRAM_CHAT_ID=-123456\n"
        "KATCHA_TELEGRAM_THREAD_ID=99\n"
    )
    monkeypatch.setattr(app, "root_private", lambda path, **kw: True)
    assert app.configuration(config) == ("123:secret-Bot_token", "-123456", "99")
    config.write_text(config.read_text() + "KATCHA_TELEGRAM_CHAT_ID=123\n")
    assert app.configuration(config) is None
    config.write_text("KATCHA_TELEGRAM_BOT_TOKEN=broken\nKATCHA_TELEGRAM_CHAT_ID=5\n")
    assert app.configuration(config) is None


def test_telegram_delivery_never_discloses_token_on_failure(monkeypatch):
    app = notify()
    token = "12345:secret-token"
    def offline(request, timeout):
        raise OSError("https://api.telegram.org/bot" + token + "/sendMessage")
    monkeypatch.setattr(app.urllib.request, "urlopen", offline)
    with pytest.raises(RuntimeError) as exc:
        app.telegram_send((token, "1234", ""), "release verified")
    assert token not in str(exc.value)


def test_notification_delivery_ledger_deduplicates(monkeypatch, tmp_path):
    app = notify()
    receipt_dir = tmp_path / "requests"
    receipt_dir.mkdir()
    (receipt_dir / ("nonce-" + "c" * 32 + ".json")).write_text(
        json.dumps({"id": 19, "sha": SHA, "status": "FAILED_REVIEW_REQUIRED"})
    )
    monkeypatch.setattr(app, "ROOT", tmp_path)
    monkeypatch.setattr(app, "RECEIPTS", receipt_dir)
    monkeypatch.setattr(app, "DELIVERED", tmp_path / "notifications")
    monkeypatch.setattr(app, "root_private", lambda path, **kw: True)
    monkeypatch.setattr(app, "configuration", lambda: ("123:token", "22", ""))
    monkeypatch.setattr(app, "live_sha", lambda: None)
    monkeypatch.setattr(app, "expired_request_events", lambda now: [])
    monkeypatch.setattr(app, "audit_confirmed", lambda sha: False)
    monkeypatch.setattr(app.os, "geteuid", lambda: 0)
    monkeypatch.setattr(app.os, "uname", lambda: SimpleNamespace(nodename=app.HOST))
    delivered = []
    monkeypatch.setattr(app, "telegram_send", lambda creds, msg: delivered.append(msg))
    assert app.run() == 0
    assert app.run() == 0
    assert len(delivered) == 1
    assert len(list(app.DELIVERED.glob("*.sent"))) == 1


def test_review_link_is_exact_run_and_requires_actual_pending_environment():
    workflow = (ROOT / ".github/workflows/production-deployment.yml").read_text()
    assert "notify-review:" in workflow
    assert "needs: preflight" in workflow
    assert "pending_deployments" in workflow
    assert 'select(.environment.name == "katcha-production")' in workflow
    assert 'actions/runs/${GITHUB_RUN_ID}' in workflow
    assert "bash scripts/notify-telegram.sh" in workflow
    assert "environment: katcha-production" in workflow
    assert "needs: preflight" in workflow
    assert "Production deployment request published" in workflow


def test_notification_timer_is_independent_and_installer_is_passive():
    service = (ROOT / "deploy/systemd/katcha-production-deploy-notify.service").read_text()
    timer = (ROOT / "deploy/systemd/katcha-production-deploy-notify.timer").read_text()
    installer = (ROOT / "deploy/scripts/install-production-deployer.sh").read_text()
    assert "NoNewPrivileges=true" in service
    assert "ProtectSystem=strict" in service
    assert "ReadWritePaths=/srv/katcha/deployments" in service
    assert "OnUnitInactiveSec=3min" in timer
    assert "production-deploy-notify.py" in installer
    assert "katcha-production-deploy-notify.timer" in installer
    assert "systemctl enable" not in installer
    assert "systemctl restart" not in installer


def test_extensionless_installed_poller_can_detect_expired_request(monkeypatch, tmp_path):
    app = notify()
    executable = tmp_path / "production-deploy-poll"
    executable.write_text(
        "def key_from_file(): return b'key'\n"
        "def fetch_requests():\n"
        "    return [{'id': 99, 'payload': {'issued_at': 1000, 'expires_at': 2200}}]\n"
        "def authorized_request(entry, key, now):\n"
        "    assert now == 2200\n"
        "    return {'id': 99, 'sha': 'a' * 40, 'nonce': 'c' * 32}\n"
    )
    monkeypatch.setattr(app, "POLLER", executable)
    monkeypatch.setattr(app, "RECEIPTS", tmp_path)
    monkeypatch.setattr(app, "root_owned_read_only", lambda _: True)
    result = app.expired_request_events(10000)
    assert len(result) == 1
    assert result[0][0] == "expired-" + "c" * 32
    assert "EXPIRED WITHOUT A HOST RECEIPT" in result[0][1]
    (tmp_path / ("nonce-" + "c" * 32 + ".json")).write_text("{}")
    assert app.expired_request_events(10000) == []


def test_success_audit_must_match_exact_release_and_deployed_state(monkeypatch, tmp_path):
    app = notify()
    audit = tmp_path / ("20261022T000000Z-" + SHA[:12])
    audit.mkdir()
    result = audit / "result.json"
    monkeypatch.setattr(app, "ROOT", tmp_path)
    monkeypatch.setattr(app, "root_private", lambda *a, **k: True)
    monkeypatch.setattr(app, "root_owned_read_only", lambda _: True)
    result.write_text(json.dumps({"status": "DEPLOYED", "sha": OTHER}))
    assert app.audit_confirmed(SHA) is False
    result.write_text(json.dumps({"status": "RECOVERY_REQUIRED", "sha": SHA}))
    assert app.audit_confirmed(SHA) is False
    result.write_text(json.dumps({"status": "DEPLOYED", "sha": SHA}))
    assert app.audit_confirmed(SHA) is True
