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
