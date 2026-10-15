"""A safe retry creates a new receipt and never replays saved actions."""

import uuid
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from test_command_goals import receipt
from test_command_goals import saved as saved_fixture

from katcha import db
from katcha.api import goals as goal_api
from katcha.api.main import app
from katcha.command_center_models import CommandActionProposal
from katcha.goal_models import CommandGoal, CommandGoalStep
from katcha.services.goal_receipts import get_goal

saved = saved_fixture


def test_retry_preserves_original_and_reuses_replacement(saved, monkeypatch):
    original = receipt(saved[0])
    with db.session_scope() as session:
        session.get(CommandGoal, original.id).status = "blocked"
    starts = []

    async def start(*args, **kwargs):
        starts.append(kwargs["id"])

    async def client():
        return SimpleNamespace(start_workflow=start)

    monkeypatch.setattr(goal_api, "get_temporal_client", client)
    with TestClient(app) as api:
        assert api.get(f"/v1/ai/goals/{original.id}").json()["retryable"]
        first = api.post(f"/v1/ai/goals/{original.id}/retry")
        second = api.post(f"/v1/ai/goals/{original.id}/retry")
    assert first.status_code == second.status_code == 202
    assert first.json()["goal_id"] == second.json()["goal_id"] != str(original.id)
    assert first.json()["command_id"] == str(uuid.uuid5(original.id, "retry"))
    assert first.json()["thread_id"] == str(original.thread_id)
    assert first.json()["request"]["prompt"] == original.request["prompt"]
    assert starts[0] == starts[1]
    assert get_goal(original.id).status == "blocked"


@pytest.mark.parametrize(
    "unsafe", ["running", "step_count", "observations", "authorization", "step", "proposal"]
)
def test_retry_rejects_saved_work(saved, unsafe):
    original = receipt(saved[0])
    with db.session_scope() as session:
        row = session.get(CommandGoal, original.id)
        row.status = "failed"
        if unsafe == "running":
            row.status = "running"
        elif unsafe == "step_count":
            row.step_count = 1
        elif unsafe == "observations":
            row.observations = [{"result": "unknown"}]
        elif unsafe == "authorization":
            row.authorization = {"mode": "run"}
        elif unsafe == "proposal":
            session.add(
                CommandActionProposal(
                    request_id=row.command_id,
                    thread_id=row.thread_id,
                    channel_profile_id=row.channel_profile_id,
                    action_type="fixture",
                    label="Saved proposal",
                    description="Must not replay",
                    payload={},
                    idempotency_key=str(uuid.uuid4()),
                    expires_at=row.deadline,
                )
            )
        else:
            session.add(CommandGoalStep(id=uuid.uuid4(), goal_id=row.id, number=1, decision={}))
    with TestClient(app) as api:
        response = api.post(f"/v1/ai/goals/{original.id}/retry")
    assert response.status_code == 409


def test_retry_requires_current_command_scope(saved, monkeypatch):
    original = receipt(saved[0])
    with db.session_scope() as session:
        session.get(CommandGoal, original.id).status = "failed"
    original_check = goal_api.require_control_scope

    def require(request, scope):
        if scope == "ai:command":
            from fastapi import HTTPException

            raise HTTPException(403, "Command access revoked")
        return original_check(request, scope)

    monkeypatch.setattr(goal_api, "require_control_scope", require)
    with TestClient(app) as api:
        assert api.post(f"/v1/ai/goals/{original.id}/retry").status_code == 403


@pytest.mark.parametrize("boundary", ["actor", "channel"])
def test_retry_enforces_receipt_owner_and_current_channel(saved, monkeypatch, boundary):
    original = receipt(saved[0])
    with db.session_scope() as session:
        session.get(CommandGoal, original.id).status = "failed"
    if boundary == "actor":
        monkeypatch.setattr(goal_api, "control_actor", lambda request: "another-actor")
    else:
        from fastapi import HTTPException

        def denied(request, channel):
            assert channel == original.channel_profile_id
            raise HTTPException(403, "Channel access revoked")

        monkeypatch.setattr(goal_api, "require_control_channel", denied)
    with TestClient(app) as api:
        assert api.post(f"/v1/ai/goals/{original.id}/retry").status_code == 403
    from sqlalchemy import select

    with db.session_scope() as session:
        assert len(list(session.scalars(select(CommandGoal)))) == 1
