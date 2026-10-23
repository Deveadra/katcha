"""Credential preflight must never start a worker or read secret bytes."""

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

SPEC = importlib.util.spec_from_file_location(
    "credentials", Path(__file__).parents[1] / "scripts/acquisition-credentials.py"
)
credentials = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(credentials)


def test_probe_is_inert_read_only_and_cleans_up(monkeypatch):
    calls = []
    monkeypatch.setattr(credentials.subprocess, "run", lambda cmd, **kw: calls.append(cmd))
    credentials.verify_mounts(
        Path("/etc/katcha-acquisition/credentials"), credentials.CF_FILES, "sha256:" + "a" * 64
    )
    run = calls[0]
    for option in (
        "--pull=never",
        "--network=none",
        "--read-only",
        "--user=10001:10001",
        "--cap-drop=ALL",
        "--entrypoint=python",
    ):
        assert option in run
    mounts = [run[i + 1] for i, arg in enumerate(run) if arg == "--mount"]
    assert len(mounts) == 2
    assert all(m.endswith(",readonly") for m in mounts)
    probe = run[run.index("-c") + 1]
    assert ".read(" not in probe and "os.read(" not in probe
    assert "os.ST_RDONLY" in probe
    assert calls[-1][:3] == ["docker", "rm", "-f"]


def test_timeout_cleans_only_checker(monkeypatch):
    calls = []

    def run(cmd, **kwargs):
        calls.append(cmd)
        if cmd[1] == "run":
            raise credentials.subprocess.TimeoutExpired(cmd, 60)

    monkeypatch.setattr(credentials.subprocess, "run", run)
    with pytest.raises(credentials.subprocess.TimeoutExpired):
        credentials.verify_mounts(
            Path("/etc/credentials"), credentials.CF_FILES, "sha256:" + "a" * 64
        )
    name = next(arg.split("=", 1)[1] for arg in calls[0] if arg.startswith("--name="))
    assert calls[1] == ["docker", "rm", "-f", name]


def test_noninteractive_install_rejected_before_input(monkeypatch):
    monkeypatch.setattr(credentials, "safe_path", lambda path: None)
    monkeypatch.setattr(credentials.sys, "stdin", SimpleNamespace(isatty=lambda: False))
    with pytest.raises(ValueError, match="interactive"):
        credentials.install(Path("/etc/credentials"), credentials.CF_FILES)


def test_symlink_directory_rejected(tmp_path):
    target = tmp_path / "real"
    target.mkdir()
    link = tmp_path / "link"
    link.symlink_to(target)
    # /tmp itself is intentionally writable; use a mocked parent chain to isolate link.
    with pytest.raises(ValueError):
        credentials.safe_path(link)


@pytest.mark.parametrize(
    "mode,uid,nlink,size",
    [
        (0o100644, 10001, 1, 10),
        (0o100600, 0, 1, 10),
        (0o120600, 10001, 1, 10),
        (0o100600, 10001, 2, 10),
        (0o100600, 10001, 1, 0),
    ],
)
def test_bad_file_metadata_rejected(monkeypatch, mode, uid, nlink, size):
    monkeypatch.setattr(credentials, "safe_path", lambda path: None)
    monkeypatch.setattr(
        Path, "stat", lambda self: SimpleNamespace(st_uid=10001, st_gid=10001, st_mode=0o40700)
    )
    monkeypatch.setattr(
        Path,
        "lstat",
        lambda self: SimpleNamespace(
            st_uid=uid, st_gid=10001, st_mode=mode, st_nlink=nlink, st_size=size
        ),
    )
    with pytest.raises(ValueError):
        credentials.check_files(Path("/etc/credentials"), credentials.CF_FILES)


def test_install_confirmation_failure_writes_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(credentials, "safe_path", lambda path: None)
    monkeypatch.setattr(credentials.sys, "stdin", SimpleNamespace(isatty=lambda: True))
    monkeypatch.setattr(credentials.sys, "stderr", SimpleNamespace(isatty=lambda: True))
    answers = iter(["fixture-one", "fixture-one", "fixture-two", "mismatch"])
    monkeypatch.setattr(credentials.getpass, "getpass", lambda prompt: next(answers))
    with pytest.raises(ValueError, match="Confirmation"):
        credentials.install(tmp_path, credentials.CF_FILES)
    assert list(tmp_path.iterdir()) == []


def test_install_private_files_and_refuse_overwrite(tmp_path, monkeypatch):
    monkeypatch.setattr(credentials, "safe_path", lambda path: None)
    monkeypatch.setattr(credentials.sys, "stdin", SimpleNamespace(isatty=lambda: True))
    monkeypatch.setattr(credentials.sys, "stderr", SimpleNamespace(isatty=lambda: True))
    monkeypatch.setattr(credentials.os, "chown", lambda *args: None)
    monkeypatch.setattr(credentials.os, "fchown", lambda *args: None)
    monkeypatch.setattr(credentials.getpass, "getpass", lambda prompt: "synthetic-test-fixture")
    credentials.install(tmp_path, credentials.CF_FILES)
    assert all((tmp_path / name).stat().st_mode & 0o777 == 0o600 for name in credentials.CF_FILES)
    with pytest.raises(ValueError, match="replace"):
        credentials.install(tmp_path, credentials.CF_FILES)
