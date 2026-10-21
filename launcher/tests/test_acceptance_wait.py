"""Bounded CI startup diagnostics without changing launcher orchestration."""

import importlib.util
import json
from pathlib import Path
from unittest.mock import Mock

import pytest

SPEC = importlib.util.spec_from_file_location(
    "launcher_acceptance", Path(__file__).with_name("acceptance.py")
)
acceptance = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(acceptance)


def _snapshot(phase, stage, events=None):
    return json.dumps({
        "phase": phase, "stage": stage, "events": events or [],
    }).encode()


def test_ready_state_is_returned_unchanged(monkeypatch):
    monkeypatch.setattr(
        acceptance, "request",
        lambda _path: _snapshot("ready", "ready"),
    )
    monkeypatch.setattr(acceptance.time, "sleep", Mock())
    state = acceptance.wait_for("ready", 10)
    assert state["phase"] == "ready"


def test_failed_background_warmup_fails_without_thirty_minute_wait(monkeypatch):
    event = {
        "level": "error", "component": "launcher",
        "message": "Docker image build failed [REDACTED]",
        "internal_secret": "do-not-expose-this-in-ci",
    }
    fetch = Mock(return_value=_snapshot(
        "degraded", "automation warm-up failed", [event],
    ))
    sleep = Mock()
    monkeypatch.setattr(acceptance, "request", fetch)
    monkeypatch.setattr(acceptance.time, "sleep", sleep)
    with pytest.raises(RuntimeError, match="automation warm-up failed") as exc:
        acceptance.wait_for("ready", 1800)
    assert "Docker image build failed" in str(exc.value)
    assert "do-not-expose-this-in-ci" not in str(exc.value)
    assert fetch.call_count == 1
    sleep.assert_not_called()


def test_nonterminal_degraded_state_can_recover(monkeypatch):
    snapshots = iter([
        _snapshot("degraded", "reattaching"),
        _snapshot("ready", "ready"),
    ])
    sleep = Mock()
    monkeypatch.setattr(acceptance, "request", lambda _path: next(snapshots))
    monkeypatch.setattr(acceptance.time, "sleep", sleep)
    assert acceptance.wait_for("ready", 10)["phase"] == "ready"
    sleep.assert_called_once_with(2)


def test_timeout_reports_last_stage_instead_of_generic_wait_failure(monkeypatch):
    monkeypatch.setattr(acceptance.time, "monotonic", iter([0, 0, 2]).__next__)
    monkeypatch.setattr(acceptance.time, "sleep", Mock())
    monkeypatch.setattr(
        acceptance, "request",
        lambda _path: _snapshot("starting", "warming automation images", [
            {"component": "launcher", "level": "info",
             "message": "Preparing missing application images"},
        ]),
    )
    with pytest.raises(TimeoutError, match="warming automation images") as exc:
        acceptance.wait_for("ready", 1)
    assert "Preparing missing application images" in str(exc.value)
