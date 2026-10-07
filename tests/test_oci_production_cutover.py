from __future__ import annotations

import hashlib
import tarfile
from pathlib import Path

import pytest

from scripts import oci_production_cutover as cutover


def test_admin_path_keeps_ssh_and_scp_port_flags_distinct(tmp_path: Path) -> None:
    admin = cutover.AdminPath(
        base=tmp_path / "bastion",
        key=tmp_path / "target-key",
        port=22022,
    )

    ssh = admin.ssh_argv("true")
    scp = admin.scp_argv("source", "ubuntu@127.0.0.1:/tmp/")

    assert "-p" in ssh
    assert "-P" not in ssh
    assert ssh[ssh.index("-p") + 1] == "22022"

    assert "-P" in scp
    assert "-p" not in scp
    assert scp[scp.index("-P") + 1] == "22022"


def test_admin_path_requires_a_listening_local_forward(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    key = tmp_path / "target-key"
    key.write_text("private", encoding="utf-8")
    admin = cutover.AdminPath(base=tmp_path / "bastion", key=key)

    called = False

    def fail_connect(*_args, **_kwargs):
        raise ConnectionRefusedError("not listening")

    def unexpected_run(*_args, **_kwargs):
        nonlocal called
        called = True
        raise AssertionError("ssh should not run before the TCP probe passes")

    monkeypatch.setattr(cutover.socket, "create_connection", fail_connect)
    monkeypatch.setattr(cutover.subprocess, "run", unexpected_run)

    with pytest.raises(cutover.CutoverError, match="not accepting TCP"):
        admin.assert_ready()

    assert called is False


def _write_snapshot(root: Path) -> tuple[str, bytes]:
    root.mkdir()
    filename = "db-example.dump"
    payload = b"postgres-cutover-dump"
    (root / filename).write_bytes(payload)
    (root / "databases.tsv").write_text(
        f"{filename}\tkatcha\n",
        encoding="utf-8",
    )
    digest = hashlib.sha256(payload).hexdigest()
    (root / "SHA256SUMS").write_text(
        f"{digest}  {filename}\n",
        encoding="utf-8",
    )
    (root / "created-at").write_text(
        "2026-10-07T12:00:00Z\n",
        encoding="utf-8",
    )
    (root / "READY").touch()
    return filename, payload


def test_snapshot_packaging_uses_manifest_members_not_shell_globs(tmp_path: Path) -> None:
    snapshot = tmp_path / "snapshot"
    filename, _payload = _write_snapshot(snapshot)

    # This would have been picked up by a careless db-*.dump glob.
    (snapshot / "db-unrelated.dump").write_bytes(b"do-not-package")

    archive = tmp_path / "cutover.tar.gz"
    cutover.package_snapshot(snapshot, archive)

    with tarfile.open(archive, "r:gz") as handle:
        names = set(handle.getnames())

    assert names == {
        "databases.tsv",
        "SHA256SUMS",
        "created-at",
        "READY",
        filename,
    }
    assert "db-unrelated.dump" not in names


def test_snapshot_packaging_fails_before_archive_when_dump_is_missing(
    tmp_path: Path,
) -> None:
    snapshot = tmp_path / "snapshot"
    filename, _payload = _write_snapshot(snapshot)
    (snapshot / filename).unlink()

    archive = tmp_path / "cutover.tar.gz"
    with pytest.raises(cutover.CutoverError, match="dump is missing"):
        cutover.package_snapshot(snapshot, archive)

    assert not archive.exists()


def test_snapshot_packaging_fails_on_checksum_mismatch(tmp_path: Path) -> None:
    snapshot = tmp_path / "snapshot"
    filename, _payload = _write_snapshot(snapshot)
    (snapshot / filename).write_bytes(b"tampered")

    with pytest.raises(cutover.CutoverError, match="checksum mismatch"):
        cutover.package_snapshot(snapshot, tmp_path / "cutover.tar.gz")
