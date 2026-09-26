import importlib.util
from pathlib import Path

import pytest


def _load_runner():
    path = Path(__file__).resolve().parents[1] / "scripts" / "ranksnaxx_private_acceptance.py"
    spec = importlib.util.spec_from_file_location("ranksnaxx_private_acceptance", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


runner = _load_runner()


def test_fixture_signals_define_five_distinct_acceptance_candidates() -> None:
    rows = [runner._fixture_signals(index) for index in range(5)]

    assert len(rows) == 5
    assert rows[0]["hook_strength"] == 96
    assert rows[-1]["payoff_strength"] == 100
    assert all(row["source_quality"] == 100 for row in rows)


def test_private_acceptance_publication_cannot_notify_or_publish() -> None:
    payload = runner._publication_payload("connection-id", "[PRIVATE ACCEPTANCE] test")

    assert payload["privacy_status"] == "private"
    assert payload["publish_at"] is None
    assert payload["notify_subscribers"] is False
    assert payload["contains_synthetic_media"] is True


def test_wait_fails_fast_when_editorial_state_never_progresses() -> None:
    with pytest.raises(RuntimeError, match="made no progress"):
        runner._wait(
            "episode editorial",
            lambda: {"status": "planned", "stage": "planned"},
            accepted={"voiced"},
            timeout_seconds=10,
            interval_seconds=0,
            no_progress_seconds=0,
            no_progress_hint="check production worker",
        )
