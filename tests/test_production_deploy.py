"""Fail-closed contracts for the production deployment executor."""
from __future__ import annotations

import importlib.util
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
    assert app.pinned(env) == (OLD, True)
    # The actual production deploy method independently blocks mixed revisions.
    assert "mixed API pin" in SOURCE.read_text()


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
    app.eligible(OLD, NEW)


def test_deployer_fails_closed_on_migration_changes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = module()
    fixture = responses(migrations=True)
    monkeypatch.setattr(app, "github", lambda p: fixture[p])
    with pytest.raises(app.Blocked, match="migration"):
        app.eligible(OLD, NEW)


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
        return ""

    monkeypatch.setattr(app, "run", fake)
    app.backup_and_restore()
    assert ("systemctl", "start", "katcha-backup.service") in operations
    assert ("systemctl", "start", "katcha-restore-test.service") in operations
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


def test_workflow_is_manual_gated_and_runner_is_environment_protected() -> None:
    source = (ROOT / ".github/workflows/production-deployment.yml").read_text()
    assert "workflow_dispatch:" in source
    assert "environment: katcha-production" in source
    assert "runs-on: [self-hosted, Linux, ARM64, katcha-production]" in source
    assert "KATCHA_PRODUCTION_DEPLOY_ENABLED" in source
    assert "cancel-in-progress: false" in source
    assert "sudo -n /usr/local/sbin/katcha-production-deploy" in source
    assert "oci instance-agent" not in source
    assert "OCI_API_PRIVATE_KEY" not in source
    assert "repository_dispatch" not in source
