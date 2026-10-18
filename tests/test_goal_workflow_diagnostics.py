"""Goal workflow status must never hide dispatch/runner failures or leak credentials."""

import uuid
from types import SimpleNamespace

from fastapi.testclient import TestClient
from temporalio.client import WorkflowExecutionStatus
from test_command_goals import receipt
from test_command_goals import saved as saved_fixture

from katcha import db
from katcha.api import goals as goal_api
from katcha.api.main import app
from katcha.goal_models import CommandGoal
from katcha.services.goal_receipts import get_goal

saved = saved_fixture


def test_dispatch_error_survives_status_reads_and_same_receipt_recovers(saved, monkeypatch):
    goal = receipt(saved[0], "Find two clips")
    workflow_ids = []

    async def unavailable(*args, **kwargs):
        workflow_ids.append(kwargs["id"])
        raise RuntimeError("connection failed; authorization=secret-token")

    async def failed_client():
        return SimpleNamespace(start_workflow=unavailable)

    monkeypatch.setattr(goal_api, "get_temporal_client", failed_client)
    payload = {**goal.request, "command_id": str(goal.command_id)}
    with TestClient(app) as api:
        first = api.post("/v1/ai/goals", json=payload)
        second = api.post("/v1/ai/goals", json=payload)
    assert first.status_code == second.status_code == 202
    for response in (first, second):
        data = response.json()
        assert data["status"] == "queued"
        assert data["dispatch_pending"] is True
        assert data["diagnostic"]["code"] == "goal_dispatch_unavailable"
        assert data["diagnostic"]["stage"] == "workflow_dispatch"
        assert data["diagnostic"]["error_type"] == "RuntimeError"
        assert data["diagnostic"]["workflow_id"] == "command-goal-" + str(goal.id)
        assert "secret-token" not in response.text
    assert get_goal(goal.id).error == "goal_dispatch_unavailable (RuntimeError)"
    assert workflow_ids == ["command-goal-" + str(goal.id)] * 2

    async def started(*args, **kwargs):
        workflow_ids.append(kwargs["id"])

    async def healthy_client():
        return SimpleNamespace(start_workflow=started)

    monkeypatch.setattr(goal_api, "get_temporal_client", healthy_client)
    with TestClient(app) as api:
        recovered = api.post("/v1/ai/goals", json=payload)
    assert recovered.status_code == 202
    assert recovered.json()["status"] == "running"
    assert recovered.json().get("dispatch_pending") is None
    assert get_goal(goal.id).error is None
    with db.session_scope() as session:
        assert session.query(CommandGoal).count() == 1


def test_status_probe_failure_is_visible_without_marking_goal_failed(saved, monkeypatch):
    goal = receipt(saved[0], "Find clip")
    with db.session_scope() as session:
        session.get(CommandGoal, goal.id).status = "running"

    async def unable_to_describe():
        raise TimeoutError("password=do-not-expose")

    async def client():
        return SimpleNamespace(
            get_workflow_handle=lambda workflow_id: SimpleNamespace(
                describe=unable_to_describe,
            )
        )

    monkeypatch.setattr(goal_api, "get_temporal_client", client)
    with TestClient(app) as api:
        response = api.get(f"/v1/ai/goals/{goal.id}")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "running"
    assert body["workflow_status_unverified"] is True
    assert body["diagnostic"]["code"] == "goal_workflow_status_unverified"
    assert body["diagnostic"]["stage"] == "workflow_status"
    assert body["diagnostic"]["error_type"] == "TimeoutError"
    assert body["diagnostic"]["workflow_id"] == "command-goal-" + str(goal.id)
    assert "do-not-expose" not in response.text
    assert get_goal(goal.id).status == "running"


def test_failed_temporal_workflow_exposes_sanitized_nested_cause(saved, monkeypatch):
    goal = receipt(saved[0], "Retrieve clip")
    with db.session_scope() as session:
        session.get(CommandGoal, goal.id).status = "running"

    async def describe():
        return SimpleNamespace(status=WorkflowExecutionStatus.FAILED)

    async def result():
        try:
            raise ValueError("download: upstream refused video")
        except ValueError as exc:
            raise RuntimeError("Child Workflow execution failed") from exc

    async def client():
        return SimpleNamespace(
            get_workflow_handle=lambda workflow_id: SimpleNamespace(
                describe=describe, result=result,
            )
        )

    monkeypatch.setattr(goal_api, "get_temporal_client", client)
    with TestClient(app) as api:
        response = api.get(f"/v1/ai/goals/{goal.id}")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "failed"
    assert "upstream refused video" in body["error"]
    assert body["diagnostic"]["code"] == "goal_failed"
    assert "upstream refused video" in body["diagnostic"]["message"]
    assert get_goal(goal.id).status == "failed"
