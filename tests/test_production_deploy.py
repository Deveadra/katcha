"""Fail-closed contracts for the production deployment executor."""
from __future__ import annotations

import hashlib
import hmac
import importlib.util
import json
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "deploy/scripts/production-deploy.py"


def module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("katcha_production_deployer", SOURCE)
    assert spec and spec.loader
    target = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(target)
    return target


OLD = "a" * 40
NEW = "b" * 40


def test_production_gate_is_disabled_by_default(monkeypatch: pytest.MonkeyPatch,
                                                tmp_path: Path) -> None:
    app = module()
    monkeypatch.setattr(app.os, "geteuid", lambda: 0)
    monkeypatch.setattr(app, "run", lambda *args, **kw: (
        "katcha-prod-primary-001" if args == ("hostname",) else "active"
    ))
    monkeypatch.setattr(app, "GATE", tmp_path / "not-created")
    with pytest.raises(app.Blocked, match="not enabled"):
        app.authorized()


def test_gate_rejects_nonroot_world_readable_authorization(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    app = module()
    gate = tmp_path / "enabled"
    gate.write_text("KATCHA_PRODUCTION_DEPLOY_ENABLED=true\n")
    gate.chmod(0o644)
    monkeypatch.setattr(app.os, "geteuid", lambda: 0)
    monkeypatch.setattr(app, "run", lambda *a, **kw: "katcha-prod-primary-001")
    monkeypatch.setattr(app, "GATE", gate)
    with pytest.raises(app.Blocked, match="not enabled|permissions"):
        app.authorized()


def test_release_pin_blocks_mixed_api_without_mutation(tmp_path: Path) -> None:
    app = module()
    env = tmp_path / "app.env"
    env.write_text(f"KATCHA_RELEASE_SHA={OLD}\nKATCHA_API_RELEASE_SHA={NEW}\n")
    with pytest.raises(app.Blocked, match="mixed API pin"):
        app.pinned(env)


def test_release_pin_rejects_duplicate_or_mutable_tags(tmp_path: Path) -> None:
    app = module()
    env = tmp_path / "app.env"
    env.write_text("KATCHA_RELEASE_SHA=main\n")
    with pytest.raises(app.Blocked, match="immutable"):
        app.pinned(env)
    env.write_text(f"KATCHA_RELEASE_SHA={OLD}\nKATCHA_RELEASE_SHA={NEW}\n")
    with pytest.raises(app.Blocked, match="duplicate"):
        app.pinned(env)


def responses(migrations: bool = False) -> dict[str, object]:
    ci = {
        "id": 100, "path": ".github/workflows/ci.yml", "head_sha": NEW,
        "head_branch": "main", "event": "push", "status": "completed",
        "conclusion": "success", "updated_at": "2026-10-15T10:00:00Z",
        "repository": {"full_name": "Deveadra/katcha"},
    }
    image = {
        "id": 101, "path": ".github/workflows/runtime-images.yml",
        "head_sha": NEW, "head_branch": "main", "event": "workflow_run",
        "status": "completed", "conclusion": "success",
        "run_started_at": "2026-10-15T10:02:00Z",
        "repository": {"full_name": "Deveadra/katcha"},
    }
    # Failed scheduled CI publication is NOT valid evidence of a failed image
    # release; a prior successfully published immutable SHA remains valid.
    invalid_attempt = {
        **image, "id": 102, "conclusion": "failure",
        "run_started_at": "2026-10-15T12:02:00Z",
    }
    return {
        "/branches/main": {"commit": {"sha": NEW}},
        "/actions/workflows/ci.yml/runs?head_sha=" + NEW + "&per_page=100":
            {"workflow_runs": [ci], "total_count": 1},
        "/actions/workflows/runtime-images.yml/runs?head_sha=" + NEW + "&per_page=100":
            {"workflow_runs": [invalid_attempt, image], "total_count": 2},
        "/compare/" + OLD + "..." + NEW: {
            "status": "ahead", "behind_by": 0, "total_commits": 1,
            "merge_base_commit": {"sha": OLD},
            "files": [{"filename": "alembic/versions/0099.py" if migrations
                       else "src/katcha/web/launcher-bridge.js", "status": "modified"}],
        },
    }


def test_release_validation_accepts_exact_successful_publication(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = module()
    fixture = responses()
    monkeypatch.setattr(app, "github", lambda p: fixture[p])
    monkeypatch.setattr(app, "run", lambda *a, **kw: json.dumps({
        "status": "ELIGIBLE_FOR_PREDEPLOY_REVIEW", "sha": NEW,
        "ci_run_id": 100, "image_run_id": 101,
    }))
    app.eligible(OLD, NEW)


def test_deployer_fails_closed_on_migration_changes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = module()
    fixture = responses(migrations=True)
    monkeypatch.setattr(app, "github", lambda p: fixture[p])
    monkeypatch.setattr(app, "run", lambda *a, **kw: json.dumps({
        "status": "ELIGIBLE_FOR_PREDEPLOY_REVIEW", "sha": NEW,
        "ci_run_id": 100, "image_run_id": 101,
    }))
    with pytest.raises(app.Blocked, match="migration"):
        app.eligible(OLD, NEW)



def test_deployer_fails_closed_if_strong_image_provenance_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = module()
    fixture = responses()
    monkeypatch.setattr(app, "github", lambda p: fixture[p])
    monkeypatch.setattr(app, "run", lambda *a, **kw: json.dumps({
        "status": "BLOCKED", "sha": NEW,
    }))
    with pytest.raises(app.Blocked, match="provenance"):
        app.eligible(OLD, NEW)


def test_deployer_reuses_installed_accepted_provenance_checker() -> None:
    source = SOURCE.read_text()
    installer = (ROOT / "deploy/scripts/install-production-deployer.sh").read_text()
    assert "/usr/local/lib/katcha-production-deploy/release_eligibility.py" in source
    assert "src/katcha/ops/release_eligibility.py" in installer
    assert "install -o root -g root -m 0600" in installer


def test_temporal_gate_refuses_running_work_without_approval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = module()
    monkeypatch.setattr(app, "run", lambda *a, **kw: "Total: 10")
    with pytest.raises(app.Blocked, match="explicit approval"):
        app.workflows("refuse")
    assert app.workflows("approved-resume") == 10
    monkeypatch.setattr(app, "run", lambda *a, **kw: "invalid output")
    with pytest.raises(app.Blocked, match="verify"):
        app.workflows("approved-resume")


def test_backup_restoration_runs_before_service_stop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = module()
    operations: list[tuple[str, ...]] = []

    def fake(*args: str, **kwargs: object) -> str:
        operations.append(args)
        if "ActiveState" in args:
            return "inactive"
        if "Result" in args:
            return "success"
        if "ExecMainStatus" in args:
            return "0"
        if args[0] == "journalctl":
            return ("Katcha PostgreSQL backup completed: fresh-id\n"
                    "Katcha disaster restore test passed for 3 databases.")
        return ""

    monkeypatch.setattr(app, "run", fake)
    app.backup_and_restore()
    assert ("systemctl", "start", "katcha-backup.service") in operations
    assert ("systemctl", "start", "katcha-restore-test.service") in operations
    assert any(args[0] == "journalctl" for args in operations)
    assert not any(args[:3] == ("systemctl", "stop", "katcha.service")
                   for args in operations)


def test_update_pin_preserves_other_secret_settings(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    app = module()
    target = tmp_path / "katcha.env"
    backup = tmp_path / "backup"
    target.write_text(f"KATCHA_RELEASE_SHA={OLD}\nPRIVATE_KEY=never-print\n")
    target.chmod(0o600)
    monkeypatch.setattr(app, "ENV", target)
    app.update_pin(NEW, backup)
    assert target.read_text() == f"KATCHA_RELEASE_SHA={NEW}\nPRIVATE_KEY=never-print\n"
    assert backup.read_text() == f"KATCHA_RELEASE_SHA={OLD}\nPRIVATE_KEY=never-print\n"
    assert backup.stat().st_mode & 0o077 == 0
    assert target.stat().st_mode & 0o077 == 0


def test_update_pin_also_updates_existing_aligned_api_override(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    app = module()
    target = tmp_path / "katcha.env"
    backup = tmp_path / "before.env"
    target.write_text(
        f"KATCHA_RELEASE_SHA={OLD}\nKATCHA_API_RELEASE_SHA={OLD}\n"
        "SECRETS_UNCHANGED=yes\n"
    )
    target.chmod(0o600)
    monkeypatch.setattr(app, "ENV", target)
    app.update_pin(NEW, backup)
    assert f"KATCHA_RELEASE_SHA={NEW}\n" in target.read_text()
    assert f"KATCHA_API_RELEASE_SHA={NEW}\n" in target.read_text()
    assert f"KATCHA_API_RELEASE_SHA={OLD}\n" in backup.read_text()
    assert "SECRETS_UNCHANGED=yes" in target.read_text()


def test_update_pin_keeps_blank_api_override_unset(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    app = module()
    target = tmp_path / "katcha.env"
    before = tmp_path / "before.env"
    target.write_text(f"KATCHA_RELEASE_SHA={OLD}\nKATCHA_API_RELEASE_SHA=\n")
    target.chmod(0o600)
    monkeypatch.setattr(app, "ENV", target)
    app.update_pin(NEW, before)
    assert target.read_text() == f"KATCHA_RELEASE_SHA={NEW}\nKATCHA_API_RELEASE_SHA=\n"


def test_workflow_is_manual_approved_and_has_no_public_host_runner() -> None:
    source = (ROOT / ".github/workflows/production-deployment.yml").read_text()
    assert "workflow_dispatch:" in source
    assert "environment: katcha-production" in source
    assert "runs-on: [self-hosted" not in source
    assert "runs-on: ubuntu-latest" in source
    assert "deployments: write" in source
    assert "KATCHA_PRODUCTION_DEPLOY_HMAC_KEY" in source
    assert "crypto.createHmac" in source
    assert "github.rest.repos.createDeployment" in source
    assert "KATCHA_PRODUCTION_DEPLOY_ENABLED" in source
    assert "cancel-in-progress: false" in source
    assert "OCI_API_PRIVATE_KEY" not in source
    assert "sudo -n" not in source
    assert "repository_dispatch" not in source

def test_backup_does_not_accept_successful_skip_without_completion_proof(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = module()

    def fake(*args: str, **kwargs: object) -> str:
        if "ActiveState" in args:
            return "inactive"
        if "Result" in args:
            return "success"
        if "ExecMainStatus" in args:
            return "0"
        return ""  # Missing the fresh completion marker

    monkeypatch.setattr(app, "run", fake)
    with pytest.raises(app.Blocked, match="fresh backup"):
        app.backup_and_restore()


def test_staging_attests_architecture_revision_digest_and_local_image_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = module()
    image_id = "sha256:" + "e" * 64

    def fake(*args: str, **kwargs: object) -> str:
        if args[:2] == ("docker", "pull"):
            return ""
        if args[-1].startswith("{{.Os}}"):
            return "linux arm64 " + NEW
        if args[-1] == "{{json .RepoDigests}}":
            name = args[3].split("/")[-1].split(":")[0]
            return '["ghcr.io/deveadra/' + name + '@sha256:' + "f" * 64 + '"]'
        if args[-1] == "{{.Id}}":
            return image_id
        raise AssertionError(args)

    monkeypatch.setattr(app, "run", fake)
    staged = app.stage_images(NEW)
    assert set(staged) == set(app.IMAGES)
    assert set(staged.values()) == {image_id}


def test_staging_rejects_wrong_oci_architecture(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = module()
    monkeypatch.setattr(app, "run", lambda *args, **kw: (
        "linux amd64 " + NEW if args and args[-1].startswith("{{.Os}}") else ""
    ))
    with pytest.raises(app.Blocked, match="platform"):
        app.stage_images(NEW)

def test_deployer_adopts_existing_attached_supervisor_kernel_lease() -> None:
    source = SOURCE.read_text()
    assert 'SUPERVISOR_LEASE_DIR = Path("/run/katcha-production-supervisor")' in source
    assert 'SUPERVISOR_LEASE_FILE = SUPERVISOR_LEASE_DIR / "compose.lock"' in source
    assert "fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)" in source
    assert "os.O_NOFOLLOW" in source
    stopped = source.index('run("systemctl", "stop", "katcha.service"')
    verified = source.index('raise Blocked("old Compose supervisor did not fully stop")')
    leased = source.index("lease_fd = acquire_supervisor_lease()")
    released = source.index("os.close(lease_fd)")
    started = source.index('run("systemctl", "start", "katcha.service"')
    assert stopped < verified < leased < released < started
    assert 'docker", "compose", "up"' not in source

POLLER = ROOT / "deploy/scripts/production-deploy-poll.py"


def poller() -> ModuleType:
    spec = importlib.util.spec_from_file_location("katcha_production_poll", POLLER)
    assert spec and spec.loader
    target = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(target)
    return target


def signed_request() -> tuple[dict, bytes]:
    app = poller()
    key = b"\x91" * 32
    payload = {
        "version": 1, "sha": NEW, "policy": "refuse",
        "host": "katcha-prod-primary-001",
        "issued_at": 1000, "expires_at": 2200,
        "nonce": "c" * 32,
    }
    payload["signature"] = hmac.new(
        key, app.canonical(payload), hashlib.sha256,
    ).hexdigest()
    deployment = {
        "id": 101, "sha": NEW, "task": "deploy",
        "environment": "katcha-production", "payload": payload,
    }
    return deployment, key


def test_signed_production_request_accepts_only_verified_short_lived_payload() -> None:
    app = poller()
    request, key = signed_request()
    proof = app.authorized_request(request, key, 1500)
    assert proof == {"id": 101, "sha": NEW, "policy": "refuse",
                     "nonce": "c" * 32}
    assert app.authorized_request(request, key, 2200)["id"] == 101


def test_tampering_with_release_or_policy_fails_signature() -> None:
    app = poller()
    request, key = signed_request()
    request["payload"]["policy"] = "approved-resume"
    with pytest.raises(app.RequestBlocked, match="signature"):
        app.authorized_request(request, key, 1500)
    request, key = signed_request()
    request["sha"] = OLD
    with pytest.raises(app.RequestBlocked, match="assertions"):
        app.authorized_request(request, key, 1500)


def test_expired_or_misaddressed_signed_request_fails() -> None:
    app = poller()
    request, key = signed_request()
    with pytest.raises(app.RequestBlocked, match="expired"):
        app.authorized_request(request, key, 2201)
    request, key = signed_request()
    request["payload"]["host"] = "other-host"
    with pytest.raises(app.RequestBlocked, match="assertions"):
        app.authorized_request(request, key, 1500)


def test_signature_cannot_be_replayed_by_changing_github_deployment_id() -> None:
    app = poller()
    request, key = signed_request()
    first = app.authorized_request(request, key, 1500)
    request["id"] = 102
    second = app.authorized_request(request, key, 1500)
    assert first["nonce"] == second["nonce"]
    # The protected consumed receipt is indexed by signed nonce, NOT the
    # forgeable externally allocated GitHub deployment ID.
    source = POLLER.read_text()
    assert '("nonce-" + request["nonce"] + ".json")' in source
    assert '("nonce-" + verified["nonce"] + ".json")' in source
    assert "os.O_EXCL" in source


def test_disabled_poller_does_not_contact_github(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = poller()
    monkeypatch.setattr(app.os, "geteuid", lambda: 0)
    monkeypatch.setattr(app.os, "uname", lambda: type(
        "U", (), {"nodename": "katcha-prod-primary-001"})())
    monkeypatch.setattr(app, "private_root_file", lambda p: (
        (_ for _ in ()).throw(app.RequestBlocked("disabled"))
    ))
    monkeypatch.setattr(app, "fetch_requests", lambda: pytest.fail(
        "disabled poller must never call GitHub"
    ))
    assert app.poll()["status"] == "DISABLED"


def test_installer_never_changes_supervisor_or_enables_timer() -> None:
    source = (ROOT / "deploy/scripts/install-production-deployer.sh").read_text()
    assert "systemctl daemon-reload" in source
    assert "systemctl enable" not in source
    assert "systemctl restart" not in source
    assert "useradd" not in source
    assert "actions-runner" not in source
    assert "sudoers" not in source
    for unit in ("katcha-production-deploy-poll.service",
                 "katcha-production-deploy-poll.timer"):
        assert unit in source
