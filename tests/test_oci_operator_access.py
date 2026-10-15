"""Offline, no-resource-change tests for the OCI operator access wrapper."""
from __future__ import annotations

import contextlib
import importlib.util
from pathlib import Path

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
    for address in ("8.8.8.8", "198.18.0.1", "127.0.0.1"):
        values["KATCHA_OCI_PRIMARY_PRIVATE_IP"] = address
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
    monkeypatch.setattr(admin, "oci", lambda *args: "ACTIVE")
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
    monkeypatch.setattr(admin, "oci", lambda *args: "ACTIVE")
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
    monkeypatch.setattr(admin, "listening", lambda: bool(spawned))
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
    monkeypatch.setattr(admin, "admin_valid", lambda: True)
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


def test_reauth_streams_browser_prompts_to_terminal(monkeypatch, tmp_path, capsys):
    cli = tmp_path / "oci"
    cli.write_text("fixture", encoding="utf-8")
    monkeypatch.setattr(admin, "OCI", cli)
    checks = iter([False, True])
    monkeypatch.setattr(admin, "admin_valid", lambda: next(checks))
    monkeypatch.setattr(admin.sys.stdin, "isatty", lambda: True)
    captured = []

    def fake_run(argv, **kwargs):
        captured.append((argv, kwargs))
        return type("Completed", (), {"returncode": 0})()

    monkeypatch.setattr(admin.subprocess, "run", fake_run)
    admin.reauth()
    assert len(captured) == 1
    argv, kwargs = captured[0]
    assert argv[1:3] == ["session", "authenticate"]
    assert kwargs == {"check": False, "timeout": 300}
    assert "capture_output" not in kwargs
    assert "vault_admin_session: VALID" in capsys.readouterr().out


def test_interactive_auth_timeout_is_actionable(monkeypatch):
    def timeout(argv, **kwargs):
        raise admin.subprocess.TimeoutExpired(argv, 300)

    monkeypatch.setattr(admin.subprocess, "run", timeout)
    with pytest.raises(admin.AccessError, match="timed out"):
        admin.run_interactive(["oci", "session", "authenticate"])


def test_interactive_auth_nonzero_does_not_leak_output(monkeypatch):
    monkeypatch.setattr(
        admin.subprocess, "run", lambda *a, **k: type("Completed", (), {"returncode": 7})()
    )
    with pytest.raises(admin.AccessError) as error:
        admin.run_interactive(["oci", "session", "authenticate"])
    assert "exit 7" in str(error.value)


def test_interrupted_login_returns_clean_code(monkeypatch, capsys):
    monkeypatch.setattr(admin, "reauth", lambda: (_ for _ in ()).throw(KeyboardInterrupt()))
    assert admin.main(["reauth"]) == 130
    assert "interrupted by operator" in capsys.readouterr().err


def test_noninteractive_reauth_never_starts_browser(monkeypatch, tmp_path):
    cli = tmp_path / "oci"
    cli.write_text("fixture", encoding="utf-8")
    monkeypatch.setattr(admin, "OCI", cli)
    monkeypatch.setattr(admin, "admin_valid", lambda: False)
    monkeypatch.setattr(admin.sys.stdin, "isatty", lambda: False)
    monkeypatch.setattr(admin, "run_interactive", lambda *a: pytest.fail("must not run"))
    with pytest.raises(admin.AccessError, match="requires a terminal"):
        admin.reauth()


def test_wrapper_contains_no_instance_creation():
    wrapper = Path("scripts/katcha-oci-admin.sh").read_text(encoding="utf-8")
    assert "exec python3" in wrapper
    source = SOURCE.read_text(encoding="utf-8")
    assert '"create-port-forwarding"' in source
    assert '"session", "delete"' in source
    assert '"instance", "launch"' not in source
    assert '"volume", "create"' not in source


def test_expired_managed_session_can_be_replaced_without_failed_delete(monkeypatch, tmp_path):
    monkeypatch.setattr(admin, "STATE_DIR", tmp_path)
    admin.save({"pid": 135, "session_id": "ocid1.bastionsession.oc1.iad.expired"})
    monkeypatch.setattr(admin, "listening", lambda: False)
    monkeypatch.setattr(admin, "owned", lambda state: False)
    monkeypatch.setattr(admin, "oci", lambda *args: "EXPIRED")
    monkeypatch.setattr(admin, "delete_session", lambda *args: pytest.fail("already expired"))
    admin.disconnect_locked()
    assert not (tmp_path / "session.json").exists()


def test_cleanup_unknown_oci_state_does_not_silently_discard_metadata(monkeypatch, tmp_path):
    monkeypatch.setattr(admin, "STATE_DIR", tmp_path)
    admin.save({"pid": 135, "session_id": "ocid1.bastionsession.oc1.iad.pending"})
    monkeypatch.setattr(admin, "listening", lambda: False)
    monkeypatch.setattr(admin, "owned", lambda state: False)
    monkeypatch.setattr(admin, "oci", lambda *args: (_ for _ in ()).throw(
        admin.AccessError("OCI session state unavailable")
    ))
    with pytest.raises(admin.AccessError, match="unavailable"):
        admin.disconnect_locked()
    assert (tmp_path / "session.json").exists()


def test_active_managed_session_with_unhealthy_target_is_not_recreated(monkeypatch):
    monkeypatch.setattr(admin, "lock", contextlib.nullcontext)
    monkeypatch.setattr(admin, "load", lambda: {"pid": 55, "session_id":
                                             "ocid1.bastionsession.oc1.iad.owned"})
    monkeypatch.setattr(admin, "owned", lambda state: True)
    monkeypatch.setattr(admin, "handshake", lambda: False)
    monkeypatch.setattr(admin, "listening", lambda: True)
    calls = []
    def fake_oci(*args):
        calls.append(args)
        return "ACTIVE"
    monkeypatch.setattr(admin, "oci", fake_oci)
    with pytest.raises(admin.AccessError, match="inspect primary"):
        admin.connect()
    assert len(calls) == 1
    assert calls[0][:3] == ("bastion", "session", "get")


def test_bastion_allowlist_match(monkeypatch):
    import json
    monkeypatch.setattr(admin, "oci", lambda *a: json.dumps({
        "data": {"client-cidr-block-allow-list": ["1.1.1.0/24", "8.8.8.0/24"]}
    }))
    monkeypatch.setattr(admin, "run", lambda *a, **k: "8.8.8.8")
    verdict, _ = admin.bastion_client_access("ocid1.bastion.oc1.iad.test")
    assert verdict == "ALLOWED"


def test_bastion_allowlist_mismatch_does_not_leak_ips(monkeypatch):
    import json
    monkeypatch.setattr(admin, "oci", lambda *a: json.dumps({
        "data": {"client-cidr-block-allow-list": ["1.1.1.0/24"]}
    }))
    monkeypatch.setattr(admin, "run", lambda *a, **k: "8.8.8.8")
    verdict, message = admin.bastion_client_access("ocid1.bastion.oc1.iad.test")
    assert verdict == "MISMATCH"
    assert "8.8.8.8" not in message
    assert "1.1.1.0" not in message


def test_allowlist_metadata_malformed_fail_closed(monkeypatch):
    import json
    monkeypatch.setattr(admin, "oci", lambda *a: json.dumps({
        "data": {"client-cidr-block-allow-list": "8.8.8.0/24"}
    }))
    monkeypatch.setattr(admin, "run", lambda *a, **k: pytest.fail("egress query not needed"))
    with pytest.raises(admin.AccessError, match="metadata unavailable"):
        admin.bastion_client_access("ocid1.bastion.oc1.iad.test")


def test_connect_preflight_blocks_mismatch_without_session(monkeypatch, tmp_path):
    monkeypatch.setattr(admin, "STATE_DIR", tmp_path / "owned")
    monkeypatch.setattr(admin, "listening", lambda: False)
    key, hosts = tmp_path / "key", tmp_path / "hosts"
    key.touch()
    hosts.touch()
    monkeypatch.setattr(admin, "TARGET_KEY", key)
    monkeypatch.setattr(admin, "TARGET_HOSTKEYS", hosts)
    monkeypatch.setattr(admin, "target", lambda: (
        "ocid1.bastion.oc1.iad.test", "10.42.2.144"
    ))
    calls = []
    def fake_oci(*args):
        calls.append(args)
        assert args[:3] == ("bastion", "bastion", "get")
        return "ACTIVE"
    monkeypatch.setattr(admin, "oci", fake_oci)
    monkeypatch.setattr(admin, "bastion_client_access", lambda *a: (
        "MISMATCH", "Bastion client CIDR mismatch"
    ))
    monkeypatch.setattr(admin, "run", lambda *a, **k: pytest.fail("no new key/session"))
    with pytest.raises(admin.AccessError, match="Refusing to create"):
        admin.connect()
    assert len(calls) == 1


def test_doctor_reports_bastion_mismatch_without_changing_oci(monkeypatch, capsys):
    monkeypatch.setattr(admin, "status", lambda: False)
    monkeypatch.setattr(admin, "target", lambda: (
        "ocid1.bastion.oc1.iad.test", "10.42.2.144"
    ))
    monkeypatch.setattr(admin, "bastion_client_access", lambda *a: (
        "MISMATCH", "public IP outside allowlist"
    ))
    assert admin.main(["doctor"]) == 2
    assert "bastion_client_allowlist: MISMATCH" in capsys.readouterr().out


def test_unverified_egress_is_not_reported_as_mismatch(monkeypatch, capsys):
    monkeypatch.setattr(admin, "target", lambda: (
        "ocid1.bastion.oc1.iad.test", "10.42.2.144"
    ))
    def unavailable(*a):
        raise admin.AccessError("public IPv4 egress cannot be verified")
    monkeypatch.setattr(admin, "bastion_client_access", unavailable)
    assert admin.diagnose_bastion_access() == "UNVERIFIED"
    assert "UNVERIFIED" in capsys.readouterr().out


def test_repair_checks_two_independent_public_ipv4_sources(monkeypatch):
    calls = []
    def fake_run(argv, timeout=45):
        calls.append(argv)
        return "75.168.42.11"
    monkeypatch.setattr(admin, "run", fake_run)
    assert str(admin.current_public_ipv4()) == "75.168.42.11"
    assert [argv[-1] for argv in calls] == [
        "https://api.ipify.org", "https://checkip.amazonaws.com"
    ]


def test_repair_refuses_disagreeing_external_ip_sources(monkeypatch):
    answers = iter(["75.168.42.11", "75.168.42.12"])
    monkeypatch.setattr(admin, "run", lambda *a, **k: next(answers))
    with pytest.raises(admin.AccessError, match="disagree"):
        admin.current_public_ipv4()


@pytest.mark.parametrize("rules, state", [
    (["75.168.1.2/32", "8.8.8.8/32"], "ACTIVE"),
    (["75.168.0.0/16"], "ACTIVE"),
    (["75.168.1.2/32"], "DELETED"),
])
def test_repair_rejects_unexpected_allowlist_shapes(monkeypatch, rules, state):
    import json
    bastion = "ocid1.bastion.oc1.iad.existing"
    monkeypatch.setattr(admin, "oci", lambda *a: json.dumps({
        "data": {"id": bastion, "lifecycle-state": state,
                 "client-cidr-block-allow-list": rules}
    }))
    with pytest.raises(admin.AccessError, match="could not be verified"):
        admin.active_bastion_single_host_rule(bastion)


def test_repair_updates_only_existing_bastion_after_approval(monkeypatch, capsys):
    monkeypatch.setattr(admin, "lock", contextlib.nullcontext)
    monkeypatch.setattr(admin, "listening", lambda: False)
    bastion = "ocid1.bastion.oc1.iad.existing"
    monkeypatch.setattr(admin, "target", lambda: (bastion, "10.42.2.144"))
    monkeypatch.setattr(admin, "current_public_ipv4", lambda: admin.ipaddress.IPv4Address(
        "75.168.42.11"
    ))
    monkeypatch.setattr(admin.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda *a: "UPDATE")
    commands = []
    def fake_oci(*args):
        commands.append(args)
        if args[:3] == ("bastion", "bastion", "get"):
            rule = "75.168.42.11/32" if any(
                row[:3] == ("bastion", "bastion", "update") for row in commands
            ) else "75.168.1.2/32"
            import json
            return json.dumps({
                "data": {"id": bastion, "lifecycle-state": "ACTIVE",
                         "client-cidr-block-allow-list": [rule]}
            })
        if args[:3] == ("bastion", "bastion", "update"):
            assert args == ("bastion", "bastion", "update", "--bastion-id", bastion,
                            "--client-cidr-list", '["75.168.42.11/32"]', "--force")
            return ""
        pytest.fail("unauthorized OCI command")
    monkeypatch.setattr(admin, "oci", fake_oci)
    admin.repair_access()
    assert sum(a[2] == "update" for a in commands) == 1
    assert "BASTION_ALLOWLIST_UPDATED: verified exact" in capsys.readouterr().out


def test_repair_never_updates_without_tty_or_confirmation(monkeypatch):
    monkeypatch.setattr(admin, "lock", contextlib.nullcontext)
    monkeypatch.setattr(admin, "listening", lambda: False)
    monkeypatch.setattr(admin, "target", lambda: ("ocid1.bastion.oc1.iad.a",
                                                "10.42.2.144"))
    monkeypatch.setattr(admin, "active_bastion_single_host_rule",
                        lambda *a: "75.168.0.1/32")
    monkeypatch.setattr(admin, "current_public_ipv4",
                        lambda: admin.ipaddress.IPv4Address("75.168.0.2"))
    monkeypatch.setattr(admin.sys.stdin, "isatty", lambda: False)
    monkeypatch.setattr(admin, "oci", lambda *a: pytest.fail("no mutation"))
    with pytest.raises(admin.AccessError, match="interactive confirmation"):
        admin.repair_access()


def test_repair_approved_but_changed_allowlist_refuses_update(monkeypatch):
    monkeypatch.setattr(admin, "lock", contextlib.nullcontext)
    monkeypatch.setattr(admin, "listening", lambda: False)
    monkeypatch.setattr(admin, "target", lambda: ("ocid1.bastion.oc1.iad.a", "10.42.2.144"))
    previous = iter(["75.168.0.1/32", "75.168.0.3/32"])
    monkeypatch.setattr(admin, "active_bastion_single_host_rule",
                        lambda *a: next(previous))
    monkeypatch.setattr(admin, "current_public_ipv4",
                        lambda: admin.ipaddress.IPv4Address("75.168.0.2"))
    monkeypatch.setattr(admin.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda *a: "UPDATE")
    monkeypatch.setattr(admin, "oci", lambda *a: pytest.fail("no update permitted"))
    with pytest.raises(admin.AccessError, match="changed during approval"):
        admin.repair_access()


def test_repair_idempotent_if_existing_rule_current(monkeypatch, capsys):
    monkeypatch.setattr(admin, "lock", contextlib.nullcontext)
    monkeypatch.setattr(admin, "listening", lambda: False)
    monkeypatch.setattr(admin, "target", lambda: ("ocid1.bastion.oc1.iad.a", "10.42.2.144"))
    monkeypatch.setattr(admin, "active_bastion_single_host_rule",
                        lambda *a: "75.168.0.2/32")
    monkeypatch.setattr(admin, "current_public_ipv4",
                        lambda: admin.ipaddress.IPv4Address("75.168.0.2"))
    monkeypatch.setattr(admin, "oci", lambda *a: pytest.fail("no update needed"))
    admin.repair_access()
    assert "ALREADY_CURRENT" in capsys.readouterr().out


def test_repair_fails_closed_on_ambiguous_write(monkeypatch):
    monkeypatch.setattr(admin, "lock", contextlib.nullcontext)
    monkeypatch.setattr(admin, "listening", lambda: False)
    monkeypatch.setattr(admin, "target", lambda: ("ocid1.bastion.oc1.iad.a", "10.42.2.144"))
    monkeypatch.setattr(admin, "active_bastion_single_host_rule",
                        lambda *a: "75.168.0.1/32")
    monkeypatch.setattr(admin, "current_public_ipv4",
                        lambda: admin.ipaddress.IPv4Address("75.168.0.2"))
    monkeypatch.setattr(admin.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda *a: "UPDATE")
    def ambiguous(*args):
        raise admin.AccessError("provider timed out")
    monkeypatch.setattr(admin, "oci", ambiguous)
    with pytest.raises(admin.AccessError, match="outcome UNKNOWN"):
        admin.repair_access()


def test_repair_declined_confirmation_makes_no_oci_mutation(monkeypatch):
    monkeypatch.setattr(admin, "lock", contextlib.nullcontext)
    monkeypatch.setattr(admin, "listening", lambda: False)
    monkeypatch.setattr(admin, "target", lambda: ("ocid1.bastion.oc1.iad.a", "10.42.2.144"))
    monkeypatch.setattr(admin, "active_bastion_single_host_rule",
                        lambda *a: "75.168.0.1/32")
    monkeypatch.setattr(admin, "current_public_ipv4",
                        lambda: admin.ipaddress.IPv4Address("75.168.0.2"))
    monkeypatch.setattr(admin.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda *a: "NO")
    monkeypatch.setattr(admin, "oci", lambda *a: pytest.fail("declined update"))
    with pytest.raises(admin.AccessError, match="confirmation not received"):
        admin.repair_access()


def test_audit_expired_admin_does_not_run_iam_or_capacity(monkeypatch, capsys):
    monkeypatch.setattr(admin, "admin_valid", lambda: False)
    monkeypatch.setattr(admin, "handshake", lambda: False)
    monkeypatch.setattr(admin, "run", lambda *a, **k: pytest.fail(
        "invalid administrator token must not invoke read-only OCI metadata probes"
    ))
    assert admin.audit() is False
    out = capsys.readouterr().out
    assert "audit_admin_session: NEEDS_BROWSER_LOGIN" in out
    assert "IAM: BLOCKED (administrator session invalid or unavailable" in out
    assert "CAPACITY: BLOCKED (administrator session invalid or unavailable" in out
    assert "recovery-readiness.py" not in out
    assert "Overall read-only evidence: INCOMPLETE" in out


def test_audit_expired_admin_still_runs_host_backup_audit(monkeypatch, capsys):
    monkeypatch.setattr(admin, "admin_valid", lambda: False)
    monkeypatch.setattr(admin, "handshake", lambda: True)
    commands = []

    def fake_run(args, timeout=45):
        commands.append(args)
        assert args[-1] == "--backups-only"
        return (
            "=== OCI HOST BACKUP/RESTORE EVIDENCE (READ ONLY) ===\n"
            "durable_mount: PASS\n"
            "katcha-backup: TIMER_ACTIVE_ENABLED evidence=SUCCESS_FRESH\n"
            "katcha-restore-test: TIMER_ACTIVE_ENABLED evidence=SUCCESS_FRESH\n"
            "READ_ONLY_AUDIT_FINISHED"
        )

    monkeypatch.setattr(admin, "run", fake_run)
    assert admin.audit() is False
    assert len(commands) == 1
    out = capsys.readouterr().out
    assert "BACKUPS" in out
    assert "durable_mount: PASS" in out
    assert "Overall read-only evidence: INCOMPLETE" in out


def test_audit_valid_admin_with_failed_probe_prints_direct_command(monkeypatch, capsys):
    monkeypatch.setattr(admin, "admin_valid", lambda: True)
    monkeypatch.setattr(admin, "handshake", lambda: False)
    monkeypatch.setattr(admin, "run", lambda *a, **k: (
        (_ for _ in ()).throw(admin.AccessError("command failed: python3 (exit 2)"))
    ))
    assert admin.audit() is False
    out = capsys.readouterr().out
    assert "python3 scripts/audit-oci-recovery-readiness.py --iam-only" in out
    assert "python3 scripts/audit-oci-free-candidate-capacity.py" in out



def test_ssh_negotiation_longer_than_two_seconds_keeps_same_child(monkeypatch):
    clock = [0.0]
    child = type("Child", (), {"poll": lambda self: None})()
    monkeypatch.setattr(admin.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(
        admin.time, "sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds)
    )
    monkeypatch.setattr(admin, "listening", lambda: clock[0] >= 3.0)
    assert admin.wait_for_forward_listener(child)
    assert clock[0] == 3.0


def test_ssh_listener_wait_is_bounded(monkeypatch):
    clock = [0.0]
    child = type("Child", (), {"poll": lambda self: None})()
    monkeypatch.setattr(admin.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(
        admin.time, "sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds)
    )
    monkeypatch.setattr(admin, "listening", lambda: False)
    assert not admin.wait_for_forward_listener(child, timeout=4.0)
    assert clock[0] == 4.0


def test_ssh_exit_stops_wait_without_accepting_unrelated_listener(monkeypatch):
    child = type("Child", (), {"poll": lambda self: 255})()
    monkeypatch.setattr(
        admin, "listening", lambda: pytest.fail("exited child cannot own the tunnel")
    )
    monkeypatch.setattr(admin.time, "sleep", lambda seconds: pytest.fail("do not wait after exit"))
    assert not admin.wait_for_forward_listener(child)
