"""Source commands must run on the launcher and report preparation outcomes."""

import ast
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from katcha.orchestration import discovery_workflows


def test_launcher_registers_command_source_workflow():
    tree = ast.parse(Path("src/katcha/orchestration/intelligence_worker.py").read_text())
    discovery_worker = next(
        call
        for call in ast.walk(tree)
        if isinstance(call, ast.Call)
        and isinstance(call.func, ast.Name)
        and call.func.id == "Worker"
        and any(
            keyword.arg == "task_queue"
            and isinstance(keyword.value, ast.Name)
            and keyword.value.id == "DISCOVERY_TASK_QUEUE"
            for keyword in call.keywords
        )
    )
    registrations = {
        keyword.arg: {item.id for item in keyword.value.elts}
        for keyword in discovery_worker.keywords
        if keyword.arg in {"workflows", "activities"}
    }
    assert "CommandSourcePrepareWorkflow" in registrations["workflows"]
    assert {
        "prepare_command_discovery_candidates_activity",
        "record_command_source_prepare_lifecycle_activity",
    } <= registrations["activities"]


@pytest.mark.parametrize(
    ("preparation", "expected_state", "expected_error_count"),
    [
        ({"prepared": [], "errors": [], "skipped": False}, "failed", 0),
        ({"prepared": [], "errors": [{"error": "rights blocked"}], "skipped": False}, "failed", 1),
        ({"prepared": [], "skipped": True}, "completed", 0),
        (
            {
                "prepared": [{"source_id": "source", "clip_id": "clip"}],
                "errors": [],
                "skipped": False,
            },
            "completed",
            0,
        ),
    ],
)
async def test_source_preparation_terminal_state(
    monkeypatch,
    preparation,
    expected_state,
    expected_error_count,
):
    activity = AsyncMock(side_effect=[preparation, {"recorded": True}])
    monkeypatch.setattr(discovery_workflows.workflow, "execute_activity", activity)
    monkeypatch.setattr(
        discovery_workflows.workflow,
        "execute_child_workflow",
        AsyncMock(return_value={"candidate_count": 1}),
    )
    monkeypatch.setattr(
        discovery_workflows.workflow,
        "info",
        lambda: SimpleNamespace(workflow_id="prepare-1"),
    )
    await discovery_workflows.CommandSourcePrepareWorkflow().run("run-1", "ingest")
    args = activity.call_args.kwargs["args"]
    assert args[2] == expected_state
    assert args[3]["preparation_error_count"] == expected_error_count
    assert bool(args[3]["error"]) == (expected_state == "failed")
