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
