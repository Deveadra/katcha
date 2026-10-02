"""Saved-data and API integration. Scripted decisions do not prove live comprehension."""

import hashlib
import uuid
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from katcha import db
from katcha.ai.goal_planner import GoalDecision
from katcha.api import goals as goal_api
from katcha.api.main import app
from katcha.command_center_models import CommandActionProposal
from katcha.config import Settings
from katcha.goal_models import CommandGoal, CommandGoalStep
from katcha.intelligence_models import ChannelProfile
from katcha.services import goal_receipts, goal_runner
from katcha.services.goal_receipts import get_goal, register_goal, resolve_goal_authority
from katcha.services.goal_tools import TOOLS, tool_schema


@pytest.fixture
def saved(monkeypatch):
    db.load_model_metadata()
    engine = create_engine(
        "sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    db.Base.metadata.create_all(engine)
    monkeypatch.setattr(db, "SessionLocal", sessionmaker(bind=engine, expire_on_commit=False))
    settings = Settings(_env_file=None, env="test", control_api_token=None, control_principals=[])
    monkeypatch.setattr(goal_receipts, "get_settings", lambda: settings)
    monkeypatch.setattr("katcha.api.control_auth.get_settings", lambda: settings)
    channel = uuid.uuid4()
    with db.session_scope() as session:
        session.add(
            ChannelProfile(
                id=channel,
                youtube_connection_id=uuid.uuid4(),
                status="active",
                timezone="UTC",
                profile_metadata={},
            )
        )
    yield channel, settings
    engine.dispose()


def receipt(channel, prompt="Set up an ongoing watch for Xbox releases", scopes=None):
    return register_goal(
        uuid.uuid4(),
        {
            "channel_profile_id": str(channel),
            "prompt": prompt,
            "thread_id": None,
            "selected_clip_ids": [],
            "resource_refs": [],
            "selected_production_id": None,
        },
        {
            "actor": "local-development",
            "scopes": scopes or ["*"],
            "channel_ids": ["*"],
            "principal_name": None,
            "credential_id": None,
            "credential_fingerprint": None,
        },
    )


def decision(tool=None, arguments=None, *, outcome="tool", allowed=None, mode="run"):
    return GoalDecision(
        outcome=outcome,
        reason="Resolve the requested operation",
        answer="Observed work is complete" if outcome == "complete" else "",
        tool=tool,
        arguments=arguments or {},
        mode=mode,
        allowed_mutations=allowed or [],
        completion_criteria="Watch saved",
        confidence=0.99,
    )


def test_retry_receipt_reuses_identity_and_rejects_changed_instruction(saved):
    goal = receipt(saved[0])
    assert register_goal(goal.command_id, goal.request, goal.authority).id == goal.id
    with pytest.raises(ValueError, match="different instruction"):
        register_goal(goal.command_id, {**goal.request, "prompt": "Different work"}, goal.authority)
    with db.session_scope() as session:
        assert len(list(session.scalars(select(CommandGoal)))) == 1


def test_catalog_native_schemas_resolve_registered_operations():
    for name in TOOLS:
        assert tool_schema(name)["type"] == "object"
    assert TOOLS["publish_production"].confirm
    assert not TOOLS["publish_production"].retry_safe
    assert "path" in tool_schema("save_watch")["properties"]


async def test_native_write_then_replan_uses_saved_observation(saved, monkeypatch):
    goal = receipt(saved[0])
    seen = []
    choices = [
        decision("channel_state", allowed=["save_watch"]),
        decision(
            "save_watch",
            {"body": {"watch_key": "xbox", "name": "Xbox releases", "include_terms": ["Xbox"]}},
        ),
        decision(outcome="complete"),
    ]

    def planner(**kwargs):
        seen.append(kwargs["context"])
        return choices.pop(0)

    monkeypatch.setattr(goal_runner, "decide_goal", planner)
    assert await goal_runner.advance_goal(goal.id) == "running"
    assert await goal_runner.advance_goal(goal.id) == "running"
    current = get_goal(goal.id)
    assert current.observations[-1]["error"] is None, current.observations[-1]
    assert current.observations[-1]["result"]["name"] == "Xbox releases"
    assert current.observations[-1]["result"]["channel_profile_id"] == str(saved[0])
    assert await goal_runner.advance_goal(goal.id) == "completed"
    assert seen[-1]["observations"][-1]["result"]["name"] == "Xbox releases"
    assert current.authorization["allowed_mutations"] == ["save_watch"]


async def test_observations_cannot_expand_authorized_mutations(saved, monkeypatch):
    goal = receipt(saved[0])
    choices = [decision("channel_state"), decision("save_watch", allowed=["save_watch"])]
    monkeypatch.setattr(goal_runner, "decide_goal", lambda **kwargs: choices.pop(0))
    assert await goal_runner.advance_goal(goal.id) == "running"
    assert await goal_runner.advance_goal(goal.id) == "blocked"
    assert "frozen" in get_goal(goal.id).summary


async def test_proposal_mode_and_cancel_stop_frozen_action(saved, monkeypatch):
    goal = receipt(saved[0])
    monkeypatch.setattr(
        goal_runner,
        "decide_goal",
        lambda **kwargs: decision(
            "save_watch",
            {"body": {"watch_key": "xbox", "name": "Xbox"}},
            allowed=["save_watch"],
            mode="propose",
        ),
    )
    assert await goal_runner.advance_goal(goal.id) == "waiting_confirmation"
    with db.session_scope() as session:
        step = session.scalar(select(CommandGoalStep).where(CommandGoalStep.goal_id == goal.id))
        proposal = session.get(CommandActionProposal, step.proposal_id)
        session.expunge(proposal)
        current = session.get(CommandGoal, goal.id)
        current.status = "cancelled"
    with pytest.raises(ValueError, match="stopped"):
        goal_runner.validate_goal_proposal(proposal, "local-development")
    assert await goal_runner.advance_goal(goal.id) == "cancelled"


async def test_uncertain_mutation_is_not_repeated(saved, monkeypatch):
    goal = receipt(saved[0])
    args = {"body": {"watch_key": "xbox", "name": "Xbox"}}
    with db.session_scope() as session:
        current = session.get(CommandGoal, goal.id)
        current.authorization = {"mode": "run", "allowed_mutations": ["save_watch"]}
        current.observations = [
            {
                "step": 0,
                "tool": "save_watch",
                "arguments": args,
                "result": {"uncertain": True},
                "error": "connection lost",
            }
        ]
        current.step_count = 1
    monkeypatch.setattr(goal_runner, "decide_goal", lambda **kwargs: decision("save_watch", args))
    assert await goal_runner.advance_goal(goal.id) == "blocked"
    assert "uncertain" in get_goal(goal.id).summary


def test_legacy_secret_revalidation_and_revocation(saved):
    goal = receipt(saved[0])
    token = "fixture-token-for-durable-goals"
    saved[1].control_api_token = SecretStr(token)
    digest = hashlib.sha256(token.encode()).hexdigest()[:12]
    goal.authority = {
        **goal.authority,
        "actor": f"control-token:{digest}",
        "credential_fingerprint": digest,
    }
    scopes, resolved = resolve_goal_authority(goal)
    assert resolved == token
    assert scopes
    saved[1].control_api_token = SecretStr("rotated-fixture-token")
    with pytest.raises(ValueError, match="changed"):
        resolve_goal_authority(goal)


def test_saved_api_retry_dispatches_same_workflow_and_cancel(saved, monkeypatch):
    starts = []

    async def start(*args, **kwargs):
        starts.append(kwargs["id"])

    async def client():
        return SimpleNamespace(start_workflow=start)

    monkeypatch.setattr(goal_api, "get_temporal_client", client)
    request = {
        "command_id": str(uuid.uuid4()),
        "channel_profile_id": str(saved[0]),
        "prompt": "Please inspect my channel",
    }
    with TestClient(app) as api:
        first = api.post("/v1/ai/goals", json=request)
        second = api.post("/v1/ai/goals", json=request)
        assert first.status_code == second.status_code == 202
        assert first.json()["goal_id"] == second.json()["goal_id"]
        assert starts[0] == starts[1]
        goal_id = first.json()["goal_id"]
        assert api.post(f"/v1/ai/goals/{goal_id}/cancel").json()["status"] == "cancelled"
        assert api.get(f"/v1/ai/goals/{goal_id}").json()["status"] == "cancelled"


async def test_background_running_waits_before_next_model_decision(saved, monkeypatch):
    from temporalio.client import WorkflowExecutionStatus

    goal = receipt(saved[0])
    with db.session_scope() as session:
        session.add(
            CommandGoalStep(
                id=uuid.uuid4(),
                goal_id=goal.id,
                number=0,
                decision=decision(
                    "refresh_intelligence", allowed=["refresh_intelligence"]
                ).model_dump(),
                status="waiting_workflow",
                result={"workflow_id": "fixture-workflow"},
            )
        )

    async def describe():
        return SimpleNamespace(status=WorkflowExecutionStatus.RUNNING)

    async def client():
        return SimpleNamespace(get_workflow_handle=lambda _: SimpleNamespace(describe=describe))

    monkeypatch.setattr("katcha.orchestration.client.get_temporal_client", client)
    monkeypatch.setattr(goal_runner, "decide_goal", lambda **kwargs: pytest.fail("Too early"))
    assert await goal_runner.advance_goal(goal.id) == "waiting_workflow"
    assert get_goal(goal.id).step_count == 0
