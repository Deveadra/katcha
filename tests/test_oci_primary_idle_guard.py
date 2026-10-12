"""The OCI A1 idle safeguard never touches production in unit tests."""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

SOURCE = Path("scripts/oci-primary-idle-guard.py")
SPEC = importlib.util.spec_from_file_location("a1_idle_guard", SOURCE)
assert SPEC and SPEC.loader
guard = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(guard)


def test_guard_is_bounded_and_low_priority():
    assert guard.RESERVE_MIB == 1408
    assert "MemoryMax=1800M" in guard.UNIT_SOURCE
    assert "MemorySwapMax=0" in guard.UNIT_SOURCE
    assert "CPUQuota=10%" in guard.UNIT_SOURCE
    assert "OOMScoreAdjust=900" in guard.UNIT_SOURCE
    assert "NoNewPrivileges=yes" in guard.UNIT_SOURCE
    assert "PrivateNetwork=yes" in guard.UNIT_SOURCE
    assert "ProtectSystem=strict" in guard.UNIT_SOURCE
    assert "STOP_AVAILABLE_MIB = 4096" in guard.DAEMON_SOURCE
    assert "START_AVAILABLE_MIB = 6144" in guard.DAEMON_SOURCE
    assert "regions.clear()" in guard.DAEMON_SOURCE


def test_operator_uses_pinned_target_key_only(monkeypatch, tmp_path):
    home = tmp_path / "home"
    key = home / ".ssh/katcha-oci"
    known = home / ".config/katcha/production/bastion/target_known_hosts"
    key.parent.mkdir(parents=True)
    known.parent.mkdir(parents=True)
    key.write_text("fixture")
    known.write_text("fixture")
    monkeypatch.setattr(guard.Path, "home", lambda: home)
    calls = []

    def fake_run(args, **kwargs):
        calls.append((args, kwargs))
        return type("Result", (), {"returncode": 0, "stdout": "GUARD_ACTIVE: test"})()

    monkeypatch.setattr(guard.subprocess, "run", fake_run)
    assert guard.operator_main("install") == 0
    args, kwargs = calls[0]
    assert args[0] == "ssh"
    assert "StrictHostKeyChecking=yes" in args
    assert "UserKnownHostsFile=" + str(known) in args
    assert "-p" in args and args[args.index("-p") + 1] == "22022"
    assert args[-3:] == ["-", "--remote", "install"]
    assert "sudo" in args
    assert kwargs["timeout"] == 120
    assert "capture_output" not in kwargs or kwargs["capture_output"] is True


def test_no_pinned_ssh_key_never_contacts_host(monkeypatch, tmp_path):
    monkeypatch.setattr(guard.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(guard.subprocess, "run", lambda *args, **kwargs: pytest.fail(
        "SSH must not be invoked"
    ))
    assert guard.operator_main("install") == 2


def test_host_rejects_wrong_identity_before_changes(monkeypatch):
    monkeypatch.setattr(guard.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(guard, "command", lambda *a, **k: pytest.fail(
        "cannot run host services"
    ))
    with pytest.raises(RuntimeError, match="sudo root"):
        guard.host_identity()


def test_insufficient_memory_blocks_host_install(monkeypatch):
    monkeypatch.setattr(guard, "host_identity", lambda: (_ for _ in ()).throw(
        RuntimeError("insufficient MemAvailable")
    ))
    monkeypatch.setattr(guard, "write_exact_file", lambda *a: pytest.fail(
        "no files may be installed"
    ))
    with pytest.raises(RuntimeError, match="insufficient"):
        guard.install_on_host()


def test_no_overwrite_of_existing_unknown_file(tmp_path):
    path = tmp_path / "guard.py"
    path.write_text("unrelated")
    with pytest.raises(RuntimeError, match="differs"):
        guard.write_exact_file(path, "expected", 0o644)
    assert path.read_text() == "unrelated"


def test_new_install_file_is_atomic_and_idempotent(tmp_path):
    path = tmp_path / "subdir/guard.py"
    assert guard.write_exact_file(path, "safe script", 0o644) is True
    assert guard.write_exact_file(path, "safe script", 0o644) is False
    assert path.read_text() == "safe script"
    assert not path.with_suffix(".py.new").exists()


def test_symlink_destination_refused(tmp_path):
    destination = tmp_path / "real"
    destination.write_text("do not touch")
    link = tmp_path / "guard.py"
    link.symlink_to(destination)
    with pytest.raises(RuntimeError, match="unexpected guard"):
        guard.write_exact_file(link, "unsafe", 0o644)
    assert destination.read_text() == "do not touch"


def test_remote_install_uses_only_expected_systemd_and_files(monkeypatch, tmp_path):
    import itertools

    script = tmp_path / "katcha-idle-guard.py"
    unit = tmp_path / "katcha-idle-guard.service"
    monkeypatch.setattr(guard, "HOST_SCRIPT", script)
    monkeypatch.setattr(guard, "HOST_UNIT", unit)
    monkeypatch.setattr(guard, "host_identity", lambda: None)
    monkeypatch.setattr(guard.time, "sleep", lambda _: None)
    monkeypatch.setattr(guard.time, "monotonic", itertools.count(step=10).__next__)
    calls = []

    def fake_command(*args, **kwargs):
        calls.append(args)
        if args[:2] == ("systemctl", "is-active") and "--quiet" not in args:
            return "active"
        if args[:2] == ("systemctl", "show"):
            return str(1450 * guard.MIB)
        return ""

    monkeypatch.setattr(guard, "command", fake_command)
    monkeypatch.setattr(guard, "show_host_status", lambda: None)
    guard.install_on_host()
    assert script.read_text() == guard.DAEMON_SOURCE
    assert unit.read_text() == guard.UNIT_SOURCE
    assert ("systemctl", "enable", "--now", "katcha-idle-guard.service") in calls
    assert not any("katcha.service" in cmd and "restart" in cmd for cmd in calls)


def test_unready_guard_is_disabled_not_claimed_protected(monkeypatch, tmp_path):
    monkeypatch.setattr(guard, "HOST_SCRIPT", tmp_path / "guard.py")
    monkeypatch.setattr(guard, "HOST_UNIT", tmp_path / "guard.service")
    monkeypatch.setattr(guard, "host_identity", lambda: None)
    monkeypatch.setattr(guard.time, "sleep", lambda _: None)
    monkeypatch.setattr(guard.time, "monotonic", iter([0, 50]).__next__)
    calls = []

    def fake_command(*args, **kwargs):
        calls.append(args)
        if args[:2] == ("systemctl", "is-active"):
            return "inactive"
        return "0"

    monkeypatch.setattr(guard, "command", fake_command)
    with pytest.raises(RuntimeError, match="failed to establish"):
        guard.install_on_host()
    assert ("systemctl", "disable", "--now", "katcha-idle-guard.service") in calls


def test_remote_status_does_not_install(monkeypatch):
    monkeypatch.setattr(guard, "show_host_status", lambda: None)
    monkeypatch.setattr(guard, "install_on_host", lambda: pytest.fail("no install"))
    assert guard.remote_main("status") == 0
