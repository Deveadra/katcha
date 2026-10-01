"""Source commands must run on the launcher and report preparation outcomes."""

import ast
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from katcha.orchestration import discovery_workflows
from katcha.orchestration.discovery_activities import _matches_command_media


@pytest.mark.parametrize(
    ("title", "metadata", "expected"),
    [
        ("VisionQuest | Official Trailer", {}, True),
        ("VisionQuest | Official Teaser", {}, True),
        ("VisionQuest cast interview", {}, False),
        (
            "Avengers | Official Trailer",
            {"provider": "youtube", "channel_id": "visionquest"},
            False,
        ),
    ],
)
def test_trailer_preparation_requires_requested_media(title, metadata, expected):
    assert (
        _matches_command_media(
            title,
            metadata,
            ["visionquest"],
            trailers_only=True,
        )
        is expected
    )


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


def test_platform_alone_cannot_resolve_a_named_source(monkeypatch):
    import uuid
    from contextlib import contextmanager

    from katcha.api import command_center

    unrelated = SimpleNamespace(
        id=uuid.uuid4(),
        name="Other YouTube Channel",
        source_key="other-youtube",
        platform="youtube",
        adapter_key="youtube",
        adapter_version="v1",
        query_template={"channel_reference": "@other"},
    )

    @contextmanager
    def session():
        yield SimpleNamespace(scalars=lambda query: [unrelated])

    monkeypatch.setattr(command_center, "session_scope", session)
    assert (
        command_center._matched_configured_source(
            channel_profile_id=uuid.uuid4(),
            prompt="Get VisionQuest trailers from Marvel's YouTube channel",
            source_hint="Marvel",
        )
        is None
    )


def test_unresolved_named_source_does_not_start_generic_scout(monkeypatch):
    import uuid

    from katcha.ai.command_planner import CommandPlan
    from katcha.api import command_center

    monkeypatch.setattr(command_center, "_matched_configured_source", lambda **kwargs: None)
    specs = command_center._action_specs(
        command_center.CommandRequest(
            channel_profile_id=uuid.uuid4(),
            prompt="Get VisionQuest trailers from Marvel",
        ),
        "source_discovery",
        [{"kind": "source_discovery", "web_scout_ready": True}],
        plan=CommandPlan(
            intent="source_discovery",
            confidence=0.99,
            reason="Named source search",
            source_hint="Marvel",
            search_query="VisionQuest trailer",
        ),
    )
    assert specs == []
