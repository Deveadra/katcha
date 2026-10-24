from __future__ import annotations

from pathlib import Path


def test_production_supervisor_prepares_handoff_tree_for_runtime_user() -> None:
    root = Path(__file__).resolve().parents[1]
    supervisor = (root / "deploy/scripts/production-supervisor.sh").read_text(
        encoding="utf-8"
    )

    assert "runtime_uid=10001" in supervisor
    for name in ("handoff", "incoming", "processed", "failed", "receipts"):
        expected = (
            '"${DATA_ROOT}/handoff"'
            if name == "handoff"
            else f'"${{DATA_ROOT}}/handoff/{name}"'
        )
        assert expected in supervisor
    assert 'install -d -o "${runtime_uid}" -g "${runtime_uid}" -m 0750' in supervisor


def test_single_host_journal_is_bound_to_verified_durable_mount():
    import yaml

    root = Path(__file__).resolve().parents[1]
    profile = yaml.safe_load((root / 'deploy/docker-compose.single-host.yml').read_text())
    for name, directory in [('local-acquisition-state', 'acquisition-state'),
                            ('local-resource-budget', 'resource-budget')]:
        options = profile['volumes'][name]['driver_opts']
        assert options == {'type': 'none', 'o': 'bind', 'device': '/srv/katcha/' + directory}
    supervisor = (root / 'deploy/scripts/production-supervisor.sh').read_text()
    assert supervisor.index('mountpoint -q') < supervisor.index('for directory in resource-budget')
    assert '[[ -L "${DATA_ROOT}/${directory}" ]]' in supervisor


def test_production_entrypoint_runs_with_checkout_script_permissions(tmp_path):
    """Git can deliver the supervisor as 0644; systemd must still launch it."""
    import shlex
    import subprocess

    root = Path(__file__).resolve().parents[1]
    unit = (root / 'deploy/systemd/katcha.service').read_text()
    starts = [line.removeprefix('ExecStart=') for line in unit.splitlines()
              if line.startswith('ExecStart=')]
    assert len(starts) == 1
    supervisor = tmp_path / 'production-supervisor.sh'
    supervisor.write_text("#!/usr/bin/env bash\nprintf '%s' supervisor-started\n")
    supervisor.chmod(0o644)
    command = [str(supervisor) if arg == '/opt/katcha/deploy/scripts/production-supervisor.sh'
               else arg for arg in shlex.split(starts[0])]
    result = subprocess.run(command, capture_output=True, text=True, check=True)
    assert result.stdout == 'supervisor-started'
