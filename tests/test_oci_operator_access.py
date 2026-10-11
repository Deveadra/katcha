"""Offline, no-resource-change tests for the OCI operator access wrapper."""
from __future__ import annotations

import contextlib
import importlib.util
from pathlib import Path
import subprocess

import pytest

SOURCE = Path("scripts/katcha-oci-admin.py")
SPEC = importlib.util.spec_from_file_location("oci_operator_access", SOURCE)
assert SPEC and SPEC.loader
admin = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(admin)


def test_ocids_and_private_target(monkeypatch):
    assert admin.valid_ocid("ocid1.bastion.oc1.iad.ABC_123", "bastion")
    assert admin.valid_ocid("ocid1.bastionsession.oc1.iad.ABC_123", "bastionsession")
    assert not admin.valid_ocid("ocid1.instance.oc1.iad.abc", "bastion")
    assert not admin.valid_ocid("ocid1xbastion.oc1.iad.abc", "bastion")
    values = {
        "KATCHA_OCI_BASTION_ID": "ocid1.bastion.oc1.iad.abc",
        "KATCHA_OCI_PRIMARY_PRIVATE_IP": "10.42.2.144",
    }
    monkeypatch.setattr(admin, "variable", lambda key: values[key])
    assert admin.target() == ("ocid1.bastion.oc1.iad.abc", "10.42.2.144")
    values["KATCHA_OCI_PRIMARY_PRIVATE_IP"] = "8.8.8.8"
    with pytest.raises(admin.AccessError):
        admin.target()


def test_unknown_listener_blocks_new_oci_session(monkeypatch):
    monkeypatch.setattr(admin, "lock", contextlib.nullcontext)
    monkeypatch.setattr(admin, "load", lambda: None)
    monkeypatch.setattr(admin, "listening", lambda: True)
    monkeypatch.setattr(admin, "oci", lambda *args: pytest.fail("OCI must not run"))
    with pytest.raises(admin.AccessError, match="unverified listener"):
        admin.connect()


def test_existing_verified_tunnel_is_reused(monkeypatch, capsys):
    monkeypatch.setattr(admin, "lock", contextlib.nullcontext)
    monkeypatch.setattr(admin, "load", lambda: {"pid": 55, "session_id": "abc"})
    monkeypatch.setattr(admin, "owned", lambda state: True)
    monkeypatch.setattr(admin, "handshake", lambda: True)
    monkeypatch.setattr(admin, "oci", lambda *args: pytest.fail("no new session"))
    admin.connect()
    assert "reused existing" in capsys.readouterr().out


def test_missing_pinned_host_key_fails_before_session_creation(monkeypatch, tmp_path):
    monkeypatch.setattr(admin, "STATE_DIR", tmp_path / "managed")
    monkeypatch.setattr(admin, "TARGET_HOSTKEYS", tmp_path / "missing_hosts")
    monkeypatch.setattr(admin, "TARGET_KEY", tmp_path / "missing_key")
    monkeypatch.setattr(admin, "listening", lambda: False)
    monkeypatch.setattr(admin, "oci", lambda *args: pytest.fail("OCI must not run"))
    with pytest.raises(admin.AccessError, match="pinned target_known_hosts"):
        admin.connect()


def test_disconnect_never_deletes_unknown_session(monkeypatch):
    monkeypatch.setattr(admin, "load", lambda: {"pid": 120, "session_id": "ocid1.bastionsession.x"})
    monkeypatch.setattr(admin, "listening", lambda: True)
    monkeypatch.setattr(admin, "owned", lambda state: False)
    monkeypatch.setattr(admin, "delete_session", lambda x: pytest.fail("must not delete"))
    with pytest.raises(admin.AccessError, match="unrecognized listener"):
        admin.disconnect_locked()


def test_managed_disconnect_removes_only_recorded_session(monkeypatch, tmp_path):
    monkeypatch.setattr(admin, "STATE_DIR", tmp_path)
    session = "ocid1.bastionsession.oc1.iad.owned"
    admin.save({"pid": 99, "session_id": session})
    for name in ("session_key", "session_key.pub"):
        (tmp_path / name).touch()
    monkeypatch.setattr(admin, "listening", lambda: False)
    monkeypatch.setattr(admin, "owned", lambda state: False)
    deleted = []
    monkeypatch.setattr(admin, "delete_session", deleted.append)
    admin.disconnect_locked()
    assert deleted == [session]
    assert not (tmp_path / "session.json").exists()
    assert not (tmp_path / "session_key").exists()


def test_failed_remote_cleanup_keeps_managed_record(monkeypatch, tmp_path):
    monkeypatch.setattr(admin, "STATE_DIR", tmp_path)
    admin.save({"pid": 111, "session_id": "ocid1.bastionsession.oc1.iad.owned"})
    monkeypatch.setattr(admin, "listening", lambda: False)
    monkeypatch.setattr(admin, "owned", lambda state: False)
    def fail(sid):
        raise admin.AccessError("API deletion failed")
    monkeypatch.setattr(admin, "delete_session", fail)
    with pytest.raises(admin.AccessError, match="deletion failed"):
        admin.disconnect_locked()
    assert (tmp_path / "session.json").exists()


def test_connect_creates_only_scoped_bastion_session(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(admin, "STATE_DIR", tmp_path / "operator")
    (tmp_path / "operator").mkdir()
    key = tmp_path / "primary_key"
    known = tmp_path / "target_known_hosts"
    key.write_text("fixture", encoding="utf-8")
    known.write_text("fixture", encoding="utf-8")
    monkeypatch.setattr(admin, "TARGET_KEY", key)
    monkeypatch.setattr(admin, "TARGET_HOSTKEYS", known)
    monkeypatch.setattr(admin, "target", lambda: ("ocid1.bastion.oc1.iad.abc", "10.42.2.144"))
    monkeypatch.setattr(admin, "listening", lambda: True if spawned else False)
    monkeypatch.setattr(admin, "handshake", lambda: bool(spawned))
    monkeypatch.setattr(admin.time, "sleep", lambda seconds: None)
    called = []
    spawned = []
    sid = "ocid1.bastionsession.oc1.iad.abcdef"
    def mock_oci(*args):
        called.append(args)
        if args[:3] == ("bastion", "bastion", "get"):
            return "ACTIVE"
        if args[:3] == ("bastion", "session", "create-port-forwarding"):
            return sid
        if args[:3] == ("bastion", "session", "get"):
            return "ACTIVE"
        pytest.fail("unexpected OCI operation")
    monkeypatch.setattr(admin, "oci", mock_oci)
    def mock_run(args, timeout=45):
        assert args[0] == "ssh-keygen"
        Path(args[-1]).write_text("fixture", encoding="utf-8")
        Path(args[-1] + ".pub").write_text("fixture", encoding="utf-8")
        return ""
    monkeypatch.setattr(admin, "run", mock_run)
    class FakeChild:
        pid = 141414
        def poll(self):
            return None
    def mock_popen(argv, **kwargs):
        assert "-L" in argv
        assert argv[argv.index("-L") + 1] == "127.0.0.1:22022:10.42.2.144:22"
        assert kwargs["start_new_session"] is True
        spawned.append(True)
        return FakeChild()
    monkeypatch.setattr(admin.subprocess, "Popen", mock_popen)
    admin.connect()
    assert admin.load()["session_id"] == sid
    assert any(x[:3] == ("bastion", "session", "create-port-forwarding") for x in called)
    assert not any("instance" in x or "volume" in x for x in called)
    assert "CONNECTED" in capsys.readouterr().out


def test_status_reports_unknown_listener_without_killing_it(monkeypatch, capsys):
    monkeypatch.setattr(admin, "admin_valid", lambda: True)
    monkeypatch.setattr(admin, "load", lambda: None)
    monkeypatch.setattr(admin, "listening", lambda: True)
    monkeypatch.setattr(admin, "variable", lambda name: "false")
    assert admin.status() is False
    assert "UNKNOWN_LISTENER" in capsys.readouterr().out


def test_audit_does_not_assume_exit_zero_means_verified(monkeypatch, capsys):
    monkeypatch.setattr(admin, "handshake", lambda: False)
    monkeypatch.setattr(admin, "run", lambda args, timeout=45: "READ_ONLY_AUDIT_FINISHED")
    assert admin.audit() is False
    assert "INCOMPLETE" in capsys.readouterr().out


def test_run_redacts_command_stderr(monkeypatch):
    class Fake:
        returncode = 2
        stderr = "PRIVATE CREDENTIAL"
        stdout = "PRIVATE CREDENTIAL"
    monkeypatch.setattr(admin.subprocess, "run", lambda *a, **k: Fake())
    with pytest.raises(admin.AccessError) as err:
        admin.run(["oci", "example"])
    assert "PRIVATE CREDENTIAL" not in str(err.value)


def test_no_implicit_admin_browser_login(monkeypatch):
    monkeypatch.setattr(admin, "admin_valid", lambda: True)
    monkeypatch.setattr(admin, "run", lambda *a, **k: pytest.fail("should not reauthenticate"))
    admin.reauth()


def test_wrapper_contains_no_instance_creation():
    wrapper = Path("scripts/katcha-oci-admin.sh").read_text(encoding="utf-8")
    assert "exec python3" in wrapper
    source = SOURCE.read_text(encoding="utf-8")
    assert '"create-port-forwarding"' in source
    assert '"session", "delete"' in source
    assert '"instance", "launch"' not in source
    assert '"volume", "create"' not in source
