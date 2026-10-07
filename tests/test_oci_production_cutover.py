from __future__ import annotations

import hashlib
import io
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


def _mock_local_docker(
    monkeypatch: pytest.MonkeyPatch,
    *,
    services: str,
) -> None:
    def run(argv, **_kwargs):
        if argv[:5] == ["docker", "compose", "ps", "-q", "postgres"]:
            stdout = "postgres-cid\n"
        elif argv[:3] == ["docker", "inspect", "--format"]:
            stdout = "katcha\n"
        elif argv[:2] == ["docker", "ps"]:
            assert "label=com.docker.compose.project=katcha" in argv
            stdout = services
        else:
            raise AssertionError(f"unexpected Docker command: {argv}")
        return cutover.subprocess.CompletedProcess(
            args=argv,
            returncode=0,
            stdout=stdout,
            stderr="",
        )

    monkeypatch.setattr(cutover.subprocess, "run", run)


def test_local_mutation_freeze_allows_only_postgres_and_minio(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _mock_local_docker(
        monkeypatch,
        services="postgres\nminio\n",
    )

    cutover.assert_local_mutation_freeze(tmp_path)


def test_local_mutation_freeze_rejects_running_worker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _mock_local_docker(
        monkeypatch,
        services="postgres\nproduction-worker\n",
    )

    with pytest.raises(cutover.CutoverError, match="production-worker"):
        cutover.assert_local_mutation_freeze(tmp_path)


def test_local_mutation_freeze_requires_the_rollback_postgres(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def no_postgres(argv, **_kwargs):
        return cutover.subprocess.CompletedProcess(
            args=argv,
            returncode=0,
            stdout="",
            stderr="",
        )

    monkeypatch.setattr(cutover.subprocess, "run", no_postgres)

    with pytest.raises(cutover.CutoverError, match="exactly one"):
        cutover.assert_local_mutation_freeze(tmp_path)


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


def test_public_health_uses_cloudflare_compatible_user_agent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self):
            return b'{"ready":true}'

    def urlopen(request, timeout):
        captured["request"] = request
        captured["timeout"] = timeout
        return Response()

    monkeypatch.setattr(cutover.urllib.request, "urlopen", urlopen)

    cutover.wait_public_health(
        "https://app.katcha.stream/v1/health/ready",
        attempts=1,
    )

    request = captured["request"]
    assert isinstance(request, cutover.urllib.request.Request)
    assert request.get_header("User-agent") == cutover.PUBLIC_HEALTH_USER_AGENT
    assert request.get_header("Cache-control") == "no-cache"
    assert captured["timeout"] == 8


def test_public_health_preserves_cloudflare_error_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    error = cutover.urllib.error.HTTPError(
        "https://app.katcha.stream/v1/health/ready",
        403,
        "Forbidden",
        hdrs=None,
        fp=io.BytesIO(b'{"error_code":1010,"error_name":"browser_signature_banned"}'),
    )

    monkeypatch.setattr(
        cutover.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(error),
    )
    monkeypatch.setattr(cutover.time, "sleep", lambda _seconds: None)

    with pytest.raises(
        cutover.CutoverError,
        match="browser_signature_banned",
    ):
        cutover.wait_public_health(
            "https://app.katcha.stream/v1/health/ready",
            attempts=1,
        )


def _activation_args(tmp_path: Path, *, apply: bool) -> object:
    local_env = tmp_path / ".env"
    local_env.write_text(
        "\n".join(
            [
                "KATCHA_RECOVERY_ADMIN_TOKEN=admin-token",
                "KATCHA_RECOVERY_CANDIDATE_TOKEN=candidate-token",
                "KATCHA_FENCE_TOKEN=fence-token",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    return type(
        "Args",
        (),
        {
            "local_env": str(local_env),
            "recovery_url": "https://recovery.example",
            "health_url": "https://app.example/v1/health/ready",
            "deployment_id": "oci-a1-primary-001",
            "expected_active_epoch": 0,
            "release_sha": "a" * 40,
            "repo_root": str(tmp_path),
            "bastion_base": str(tmp_path / "bastion"),
            "ssh_key": str(tmp_path / "target-key"),
            "port": 22022,
            "user": "ubuntu",
            "apply": apply,
        },
    )()


def test_activation_is_inspect_only_without_apply(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    args = _activation_args(tmp_path, apply=False)
    remote_scripts: list[str] = []

    monkeypatch.setattr(cutover, "assert_local_mutation_freeze", lambda _root: None)
    monkeypatch.setattr(cutover.AdminPath, "assert_ready", lambda _self: None)

    def run_script(self, script, *, sudo, args=(), check=True):
        del self, sudo, args, check
        remote_scripts.append(script)
        return cutover.subprocess.CompletedProcess(args=[], returncode=0)

    monkeypatch.setattr(cutover.AdminPath, "run_script", run_script)

    def request_json(method, url, token, payload=None, timeout=15):
        del token, payload, timeout
        assert method == "GET"
        assert url.endswith("/v1/authority/status")
        return {"active": None, "pending": None}

    monkeypatch.setattr(cutover, "request_json", request_json)

    cutover.activate(args)

    assert remote_scripts == [cutover.REMOTE_INSPECT]


def test_precommit_rollback_leaves_only_postgres_for_safe_retry() -> None:
    script = cutover.REMOTE_STOP

    assert "systemctl stop katcha.service" in script
    assert "up -d postgres" in script
    assert "up -d api" not in script
    assert "up -d worker" not in script
    assert "up -d cloudflared" not in script


def test_precommit_failure_stops_candidate_and_aborts_pending_epoch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    args = _activation_args(tmp_path, apply=True)
    remote_scripts: list[str] = []
    requests: list[tuple[str, str]] = []

    monkeypatch.setattr(cutover, "assert_local_mutation_freeze", lambda _root: None)
    monkeypatch.setattr(cutover.AdminPath, "assert_ready", lambda _self: None)

    def run_script(self, script, *, sudo, args=(), check=True):
        del self, sudo, args, check
        remote_scripts.append(script)
        if script == cutover.REMOTE_START:
            raise cutover.subprocess.CalledProcessError(30, "remote-start")
        return cutover.subprocess.CompletedProcess(args=[], returncode=0)

    monkeypatch.setattr(cutover.AdminPath, "run_script", run_script)

    def request_json(method, url, token, payload=None, timeout=15):
        del token, timeout
        requests.append((method, url))
        if method == "GET":
            return {"active": None, "pending": None}
        if url.endswith("/v1/authority/prepare"):
            return {
                "pending": {
                    "deployment_id": "oci-a1-primary-001",
                    "epoch": 1,
                }
            }
        if url.endswith("/v1/authority/abort"):
            assert payload == {
                "deployment_id": "oci-a1-primary-001",
                "deployment_epoch": 1,
            }
            return {"aborted": True}
        raise AssertionError(f"unexpected coordinator request: {method} {url}")

    monkeypatch.setattr(cutover, "request_json", request_json)

    with pytest.raises(cutover.subprocess.CalledProcessError):
        cutover.activate(args)

    assert cutover.REMOTE_STOP in remote_scripts
    assert ("POST", "https://recovery.example/v1/authority/abort") in requests


def test_uncertain_commit_reconciles_active_leader_without_stopping_candidate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    args = _activation_args(tmp_path, apply=True)
    remote_scripts: list[str] = []
    fence_results = iter(
        [
            {"authorized": False, "active_epoch": 0},
            {"authorized": True, "active_epoch": 1},
        ]
    )

    monkeypatch.setattr(cutover, "assert_local_mutation_freeze", lambda _root: None)
    monkeypatch.setattr(cutover.AdminPath, "assert_ready", lambda _self: None)

    def run_script(self, script, *, sudo, args=(), check=True):
        del self, sudo, args, check
        remote_scripts.append(script)
        return cutover.subprocess.CompletedProcess(args=[], returncode=0)

    monkeypatch.setattr(cutover.AdminPath, "run_script", run_script)
    monkeypatch.setattr(cutover, "wait_public_health", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        cutover,
        "fence_assert",
        lambda *_args, **_kwargs: next(fence_results),
    )

    status_reads = 0

    def request_json(method, url, token, payload=None, timeout=15):
        nonlocal status_reads
        del token, payload, timeout
        if method == "GET" and url.endswith("/v1/authority/status"):
            status_reads += 1
            if status_reads == 1:
                return {"active": None, "pending": None}
            return {
                "active": {
                    "deployment_id": "oci-a1-primary-001",
                    "epoch": 1,
                }
            }
        if url.endswith("/v1/authority/prepare"):
            return {
                "pending": {
                    "deployment_id": "oci-a1-primary-001",
                    "epoch": 1,
                }
            }
        if url.endswith("/v1/authority/candidate-ready"):
            return {"ready": True}
        if url.endswith("/v1/authority/commit"):
            raise cutover.CutoverError("simulated lost commit response")
        raise AssertionError(f"unexpected coordinator request: {method} {url}")

    monkeypatch.setattr(cutover, "request_json", request_json)

    cutover.activate(args)

    assert cutover.REMOTE_STOP not in remote_scripts
    assert status_reads == 2
