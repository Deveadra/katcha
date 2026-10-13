"""Fixture-based validation of the embedded Windows/WSL Codex helper repair.

These tests use a fake /proc tree and temporary paths. They never mutate the
actual Codex installation, run Windows PowerShell, or require WSL.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts/codex-wsl-agent-preflight.ps1"
HELPER_LINKS = (
    "codex-linux-sandbox",
    "codex-execve-wrapper",
    "apply_patch",
    "applypatch",
)


def _mock_runtime(
    tmp_path: Path,
    *,
    expected_binary_layout: bool = False,
    has_app_server: bool = True,
    has_live_lock: bool = True,
) -> tuple[Path, Path, Path]:
    source = SCRIPT_PATH.read_text(encoding="utf-8")
    bash = source.split("$bash = @'\n", 1)[1].split("\n'@", 1)[0]
    root = tmp_path / ".codex"
    helper = root / "tmp/arg0/codex-arg0Mock"
    helper.mkdir(parents=True)

    executable = (
        root / "bin/wsl/example/codex"
        if expected_binary_layout
        else tmp_path / "another-codex-layout/codex"
    )
    executable.parent.mkdir(parents=True)
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o755)

    fake_proc = tmp_path / "fake-proc"
    fake_process = fake_proc / "4321"
    (fake_process / "fd").mkdir(parents=True)
    (fake_process / "exe").symlink_to(executable)
    (fake_process / "cmdline").write_bytes(
        b"/path/to/codex\0" + (b"app-server" if has_app_server else b"other") + b"\0"
    )
    (helper / ".lock").touch()
    if has_live_lock:
        (fake_process / "fd/5").symlink_to(helper / ".lock")

    # Only this test copy's /proc paths change. Production script always reads /proc.
    fixture_script = tmp_path / "fixture-repair.sh"
    fixture_script.write_text(
        bash.replace("/proc/", str(fake_proc) + "/") + "\n", encoding="utf-8"
    )
    return root, helper, fixture_script


def _run(script: Path, root: Path, mode: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(script), str(root), mode],
        capture_output=True,
        text=True,
        check=False,
    )


def test_discover_alternate_executable_layout_by_live_lock(tmp_path: Path) -> None:
    root, helper, script = _mock_runtime(tmp_path)
    result = _run(script, root, "diagnose")
    assert result.returncode == 0, result.stderr
    assert "expected-bin-layout=0 valid-helper-locks=1" in result.stdout
    assert "CODEX_WSL_HELPER_DIAGNOSTIC_COMPLETE" in result.stdout
    assert not any((helper / name).exists() for name in HELPER_LINKS)


def test_check_then_repair_then_recheck_idempotently(tmp_path: Path) -> None:
    root, helper, script = _mock_runtime(tmp_path)
    before = _run(script, root, "check")
    assert before.returncode != 0
    assert "MISSING: apply_patch" in before.stderr
    assert not any((helper / name).exists() for name in HELPER_LINKS)

    first = _run(script, root, "repair")
    assert first.returncode == 0, first.stderr
    assert first.stdout.count("PASS: ") == len(HELPER_LINKS)
    assert "CODEX_WSL_HELPER_REPAIR_OK" in first.stdout
    assert all((helper / name).is_symlink() for name in HELPER_LINKS)

    assert _run(script, root, "repair").returncode == 0
    after = _run(script, root, "check")
    assert after.returncode == 0
    assert "CODEX_WSL_HELPER_CHECK_OK" in after.stdout


def test_existing_unexpected_file_prevents_any_writes(tmp_path: Path) -> None:
    root, helper, script = _mock_runtime(tmp_path)
    (helper / "apply_patch").write_text("do not overwrite", encoding="utf-8")
    result = _run(script, root, "repair")
    assert result.returncode != 0
    assert "Existing non-symlink file" in result.stderr
    assert (helper / "apply_patch").read_text(encoding="utf-8") == "do not overwrite"
    assert not (helper / "codex-execve-wrapper").exists()


@pytest.mark.parametrize(
    ("settings", "message"),
    [
        ({"has_app_server": False}, "No app-server process is visible"),
        ({"has_live_lock": False}, "does not hold the expected live helper"),
    ],
)
def test_missing_identity_evidence_fails_without_writes(
    tmp_path: Path, settings: dict[str, bool], message: str
) -> None:
    root, helper, script = _mock_runtime(tmp_path, **settings)
    result = _run(script, root, "repair")
    assert result.returncode != 0
    assert message in result.stderr
    assert not any((helper / name).exists() for name in HELPER_LINKS)


def test_original_expected_executable_layout_still_works(tmp_path: Path) -> None:
    root, helper, script = _mock_runtime(tmp_path, expected_binary_layout=True)
    result = _run(script, root, "repair")
    assert result.returncode == 0, result.stderr
    assert "expected-bin-layout=1 valid-helper-locks=1" in result.stdout
    assert all((helper / name).is_symlink() for name in HELPER_LINKS)
