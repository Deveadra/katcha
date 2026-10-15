"""Kernel lease contracts for the production supervisor, without Docker or OCI."""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest


def test_supervisor_acquires_private_lease_before_any_compose_actions() -> None:
    root = Path(__file__).resolve().parents[1]
    source = (root / "deploy/scripts/production-supervisor.sh").read_text()
    assert "lease_dir=/run/katcha-production-supervisor" in source
    assert "install -d -o 0 -g 0 -m 0700" in source
    assert 'umask 077' in source
    assert 'exec 9>>"$lease_file"' in source
    assert 'flock --exclusive --nonblock 9' in source
    assert "Unsafe production Compose lease directory" in source
    assert "Unsafe production Compose lock path" in source
    assert "Another production Compose owner holds the lease" in source
    assert source.index('flock --exclusive --nonblock 9') < source.index(
        'PYTHONPATH="${ROOT}/src'
    )
    assert source.index('flock --exclusive --nonblock 9') < source.index(
        '"${compose[@]}" pull'
    )
    assert 'exec "${compose[@]}" up --remove-orphans' in source

    # No unit lifecycle changes are needed to reject duplicate instances
    # after supervised adoption of the new supervisor code on the host.
    unit = (root / "deploy/systemd/katcha.service").read_text()
    assert "ExecStart=/opt/katcha/deploy/scripts/production-supervisor.sh" in unit


@pytest.mark.skipif(shutil.which("flock") is None, reason="Linux flock unavailable")
def test_kernel_lock_is_held_across_exec_and_released_on_exit(tmp_path: Path) -> None:
    lock = tmp_path / "lease"
    command = (
        'exec 9>>"$1"; flock --exclusive --nonblock 9 || exit 24; '
        'printf "locked\\n"; exec sleep 20'
    )
    owner = subprocess.Popen(
        ["bash", "-c", command, "lease-test", str(lock)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert owner.stdout is not None
        assert owner.stdout.readline().strip() == "locked"
        competitor = subprocess.run(
            ["bash", "-c", 'exec 9>>"$1"; flock --exclusive --nonblock 9',
             "lease-test", str(lock)],
            check=False, capture_output=True, timeout=4,
        )
        assert competitor.returncode != 0
    finally:
        owner.terminate()
        owner.communicate(timeout=4)
    successor = subprocess.run(
        ["bash", "-c", 'exec 9>>"$1"; flock --exclusive --nonblock 9',
         "lease-test", str(lock)],
        check=False, capture_output=True, timeout=4,
    )
    assert successor.returncode == 0


def test_supervisor_syntax_still_valid() -> None:
    root = Path(__file__).resolve().parents[1]
    script = root / "deploy/scripts/production-supervisor.sh"
    process = subprocess.run(
        ["bash", "-n", str(script)],
        check=False, capture_output=True, text=True, timeout=5,
    )
    assert process.returncode == 0, process.stderr
