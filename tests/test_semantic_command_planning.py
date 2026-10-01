"""Mocked planning contracts; these tests do not evaluate live model comprehension."""

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from katcha.ai.command_center import CommandNarrative, CommandNarrativeResult
from katcha.ai.command_planner import CommandPlan, CommandPlanResult, _planner_prompt
from katcha.ai.router import ModelTarget
from katcha.api import command_center as api
from katcha.command_center_models import CommandActionProposal, CommandTurn
from katcha.services.command_planning import (
    planning_context,
    resolve_planned_clip_ids,
    resolve_planned_proposals,
)


def _proposal(channel, action_type="start_source_scout", status="proposed"):
    return CommandActionProposal(
        id=uuid.uuid4(),
        channel_profile_id=channel,
        source_turn_id=uuid.uuid4(),
        action_type=action_type,
        label=action_type,
        description="Fixture operation",
        payload={"search_query": "VisionQuest official trailer"},
        result={},
        status=status,
        expires_at=datetime.now(UTC) + timedelta(minutes=30),
    )


def test_semantic_context_contains_real_history_and_frozen_actions():
    channel, clip = uuid.uuid4(), uuid.uuid4()
    turn = CommandTurn(
        id=uuid.uuid4(),
        role="assistant",
        content="Here are the options",
        intent="best_clips",
        evidence=[{"kind": "clip", "id": str(clip)}],
        turn_context={},
    )
    proposal = _proposal(channel)
    context = planning_context([turn], [proposal], [], [])
    prompt = _planner_prompt(
        user_prompt="Put the Marvel one in motion",
        effective_prompt="Put the Marvel one in motion",
        selected_clip_count=0,
        previous_intent="best_clips",
        context=context,
    )
    assert str(proposal.id) in prompt
    assert str(clip) in prompt
    assert "Here are the options" in prompt
    assert "from meaning and conversation state" in prompt
    assert context["actions"][0]["payload"] == proposal.payload


def test_clip_references_require_grounded_records():
    clip = uuid.uuid4()
    plan = CommandPlan(
        intent="create_content", confidence=0.98, reason="Use this clip", selected_clip_ids=[clip]
    )
    assert resolve_planned_clip_ids(plan, {"explicit_clip_ids": [str(clip)]}) == [clip]
    with pytest.raises(ValueError, match="outside the grounded"):
        resolve_planned_clip_ids(plan, {})


def test_action_references_cannot_invent_or_cross_channels():
    channel = uuid.uuid4()
    proposal = _proposal(channel)
    plan = CommandPlan(
        intent="confirm_action",
        confidence=0.99,
        reason="Explicit authorization",
        proposal_ids=[proposal.id],
    )
    assert resolve_planned_proposals(plan, [proposal], channel) == [proposal]
    with pytest.raises(ValueError, match="outside this channel"):
        resolve_planned_proposals(plan, [proposal], uuid.uuid4())
    with pytest.raises(ValueError, match="outside this channel"):
        resolve_planned_proposals(plan, [], channel)
    proposal.status = "expired"
    with pytest.raises(ValueError, match="expired"):
        resolve_planned_proposals(plan, [proposal], channel)


@pytest.fixture
def command_harness(monkeypatch):
    channel = uuid.uuid4()
    thread = SimpleNamespace(id=uuid.uuid4(), channel_profile_id=channel, status="active")
    state = SimpleNamespace(
        channel=channel,
        thread=thread,
        proposals=[],
        turns=[],
        captured_context=None,
        finalized=None,
        events=[],
    )
    settings = SimpleNamespace(ai_enabled=True, resolved_ai_execution_mode=lambda: "live")
    monkeypatch.setattr(api, "get_settings", lambda: settings)
    monkeypatch.setattr(api, "require_control_channel", lambda *args: None)
    monkeypatch.setattr(api, "require_control_scope", lambda *args: None)
    monkeypatch.setattr(api, "control_actor", lambda request: "fixture-operator")
    monkeypatch.setattr(api, "control_credential_id", lambda request: None)
    monkeypatch.setattr(api, "control_credential_fingerprint", lambda request: None)
    monkeypatch.setattr(api, "get_command_thread", lambda thread_id, **kwargs: thread)
    monkeypatch.setattr(api, "create_command_thread", lambda **kwargs: thread)
    monkeypatch.setattr(api, "list_command_turns", lambda thread_id: state.turns)
    monkeypatch.setattr(api, "list_thread_proposals", lambda thread_id: state.proposals)
    monkeypatch.setattr(
        api,
        "get_action_proposal",
        lambda proposal_id: next(p for p in state.proposals if p.id == proposal_id),
    )
    monkeypatch.setattr(api, "resolve_command_resources", lambda *args: [])
    monkeypatch.setattr(api, "record_command_observation", lambda **kwargs: None)

    def plan(**kwargs):
        state.captured_context = kwargs["context"]
        return CommandPlanResult(state.plan, "ai", ModelTarget("gemini", "fixture-model"), 0, 0)

    monkeypatch.setattr(api, "plan_ambiguous_command", plan)

    def record(**kwargs):
        state.events.append("persist_turn")
        return (SimpleNamespace(id=uuid.uuid4()), SimpleNamespace(id=uuid.uuid4()))

    monkeypatch.setattr(api, "record_command_exchange", record)

    def finalize(turn_id, **kwargs):
        state.finalized = kwargs

    monkeypatch.setattr(api, "finalize_command_answer", finalize)
    monkeypatch.setattr(
        api,
        "compose_grounded_answer",
        lambda **kwargs: CommandNarrativeResult(
            CommandNarrative(answer=kwargs["deterministic_answer"]),
            ModelTarget("gemini", "fixture-model"),
            0,
            0,
        ),
    )

    def create(**kwargs):
        created = []
        for spec in kwargs["specs"]:
            proposal = _proposal(channel, spec.action_type)
            proposal.payload = spec.payload
            state.proposals.append(proposal)
            created.append(proposal)
        state.events.append("persist_proposals")
        return created

    monkeypatch.setattr(api, "create_action_proposals", create)

    def claim(proposal_id, **kwargs):
        proposal = next(p for p in state.proposals if p.id == proposal_id)
        should_execute = proposal.status not in {"executed", "executing"}
        if should_execute:
            proposal.status = "executing"
        return SimpleNamespace(proposal=proposal, should_execute=should_execute)

    monkeypatch.setattr(api, "claim_action_proposal", claim)

    async def execute(proposal, **kwargs):
        state.events.append("execute")
        return {"workflow_id": "fixture-workflow"}

    state.execute = AsyncMock(side_effect=execute)
    monkeypatch.setattr(api, "_execute_proposal", state.execute)

    def complete(proposal_id, **kwargs):
        proposal = next(p for p in state.proposals if p.id == proposal_id)
        proposal.status = "executed"
        proposal.result = kwargs["result"]
        return proposal

    monkeypatch.setattr(api, "complete_action_proposal", complete)
    monkeypatch.setattr(api, "channel_status", lambda channel: ("Fixture channel state", []))

    async def send(prompt):
        return await api.command(
            http_request=Request({"type": "http", "headers": []}),
            request=api.CommandRequest(
                channel_profile_id=channel, thread_id=thread.id, prompt=prompt
            ),
        )

    state.send = send
    return state


@pytest.mark.parametrize(
    "prompt",
    [
        "Put the Marvel search in motion",
        "Let's do the trailer search you outlined earlier",
        "The second suggestion is the one I want you to carry out",
    ],
)
async def test_semantic_follow_through_selects_frozen_action_without_phrase_match(
    command_harness,
    prompt,
):
    state = command_harness
    first, second = _proposal(state.channel), _proposal(state.channel)
    state.proposals = [first, second]
    state.plan = CommandPlan(
        intent="confirm_action", confidence=0.99, reason="Select second", proposal_ids=[second.id]
    )
    result = await state.send(prompt)
    state.execute.assert_awaited_once()
    assert state.execute.call_args.args[0].id == second.id
    assert result.actions[0].proposal_id == second.id
    assert result.actions[0].status == "executed"
    assert state.finalized["content"] == result.answer
    assert len(state.captured_context["actions"]) == 2


async def test_semantic_confirmation_retry_does_not_restart_work(command_harness):
    state = command_harness
    proposal = _proposal(state.channel, status="executed")
    state.proposals = [proposal]
    state.plan = CommandPlan(
        intent="confirm_action",
        confidence=0.99,
        reason="Repeated request",
        proposal_ids=[proposal.id],
    )
    result = await state.send("Do that trailer retrieval again if you haven't already")
    state.execute.assert_not_awaited()
    assert "no duplicate" in result.answer


async def test_negation_bypasses_old_confirmation_phrase(command_harness):
    state = command_harness
    state.proposals = [_proposal(state.channel)]
    state.plan = CommandPlan(
        intent="conversation", confidence=0.99, reason="User says no", requested_actions=[]
    )
    result = await state.send("Proceed with absolutely nothing; explain it first")
    state.execute.assert_not_awaited()
    assert result.actions == []


async def test_permissions_preflight_all_selected_actions(command_harness, monkeypatch):
    state = command_harness
    first = _proposal(state.channel)
    second = _proposal(state.channel, "create_short_production")
    state.proposals = [first, second]
    state.plan = CommandPlan(
        intent="confirm_action",
        confidence=0.99,
        reason="Do both",
        proposal_ids=[first.id, second.id],
    )

    def require(request, scope):
        if scope == "production:create":
            raise HTTPException(status_code=403, detail="fixture denied")

    monkeypatch.setattr(api, "require_control_scope", require)
    with pytest.raises(HTTPException) as caught:
        await state.send("Carry out both operations")
    assert caught.value.status_code == 403
    state.execute.assert_not_awaited()


async def test_direct_source_instruction_persists_then_starts_work(command_harness, monkeypatch):
    state = command_harness
    state.plan = CommandPlan(
        intent="source_discovery",
        confidence=0.99,
        reason="Direct instruction",
        goal="Prepare official VisionQuest trailers",
        source_hint="Marvel",
        search_query="VisionQuest official trailer",
        prepare_for_production=True,
        requested_actions=["start_source_scout"],
        execution="run",
    )
    monkeypatch.setattr(
        api,
        "source_discovery_plan",
        lambda *args: (
            "Fixture source capabilities",
            [{"kind": "source_discovery", "web_scout_ready": True}],
        ),
    )
    monkeypatch.setattr(
        api,
        "_matched_configured_source",
        lambda **kwargs: {
            "id": str(uuid.uuid4()),
            "name": "Marvel Entertainment",
        },
    )
    result = await state.send("Get VisionQuest's official trailers from Marvel and ready them")
    assert state.events[:3] == ["persist_turn", "persist_proposals", "execute"]
    assert result.actions[0].status == "executed"
    assert result.actions[0].payload["prepare_for_production"] is True
    assert "workflow started" in result.answer
    assert state.finalized["context"]["planning"]["plan"]["goal"] == state.plan.goal


async def test_multi_system_inspection_does_not_propose_unrequested_changes(
    command_harness,
    monkeypatch,
):
    state = command_harness
    state.plan = CommandPlan(
        intent="channel_status",
        confidence=0.99,
        reason="Inspect multiple systems",
        inspections=["failures", "performance_advice"],
        requested_actions=[],
    )
    monkeypatch.setattr(
        api,
        "failures",
        lambda *args: (
            "Fixture render failure",
            [{"kind": "render_attempt", "id": "render"}],
        ),
    )
    monkeypatch.setattr(
        api,
        "performance_advice",
        lambda *args: (
            "Fixture performance",
            [{"kind": "performance", "id": "performance"}],
        ),
    )
    result = await state.send("What is holding us back, and how are our videos doing?")
    assert {row["kind"] for row in result.evidence} == {"render_attempt", "performance"}
    assert result.actions == []
    state.execute.assert_not_awaited()


async def test_clarification_preserves_specific_missing_decision(command_harness):
    state = command_harness
    question = "Should I prepare the Marvel trailers or recover the failed render?"
    state.plan = CommandPlan(
        intent="clarification",
        confidence=0.95,
        reason="Two targets",
        clarification_question=question,
    )
    result = await state.send("Do that one")
    assert result.answer == question
    assert result.actions == []
    state.execute.assert_not_awaited()


async def test_observation_round_binds_newly_found_clips_to_ranked_production(
    command_harness,
    monkeypatch,
):
    state = command_harness
    clips = [uuid.uuid4() for _ in range(5)]
    initial = CommandPlan(
        intent="best_clips",
        confidence=0.99,
        reason="Find media then create a ranking",
        goal="Produce a five-clip ranking",
        requested_actions=["create_ranked_short_episode"],
        execution="run",
    )
    bound = initial.model_copy(update={"selected_clip_ids": clips})
    calls = []

    def plan(**kwargs):
        calls.append(kwargs["context"])
        value = bound if kwargs["context"].get("phase") else initial
        return CommandPlanResult(value, "ai", ModelTarget("gemini", "fixture-model"), 0, 0)

    monkeypatch.setattr(api, "plan_ambiguous_command", plan)
    monkeypatch.setattr(
        api,
        "best_clips",
        lambda *args: (
            "Fixture candidate clips",
            [{"kind": "clip", "id": str(clip)} for clip in clips],
        ),
    )
    monkeypatch.setattr(api, "ranked_episode_allowed_counts", lambda channel: [5])
    result = await state.send("Find five clips that fit our audience and make a ranking")
    assert len(calls) == 2
    assert calls[1]["phase"] == "bind_actions_after_observation"
    assert [row["id"] for row in calls[1]["observations"]] == [str(clip) for clip in clips]
    assert result.actions[0].type == "create_ranked_short_episode"
    assert result.actions[0].payload["clip_ids"] == [str(clip) for clip in clips]
    state.execute.assert_awaited_once()


async def test_unavailable_secondary_inspection_keeps_other_evidence(command_harness, monkeypatch):
    state = command_harness
    state.plan = CommandPlan(
        intent="channel_status",
        confidence=0.98,
        reason="Compare systems",
        inspections=["failures", "performance_advice"],
        requested_actions=[],
    )
    monkeypatch.setattr(
        api,
        "failures",
        lambda *args: (
            "Fixture failure evidence",
            [
                {"kind": "render_attempt", "id": "render"},
            ],
        ),
    )

    def unavailable(*args):
        raise ValueError("Fixture analytics unavailable")

    monkeypatch.setattr(api, "performance_advice", unavailable)
    result = await state.send("Compare our failures and performance")
    assert {row["kind"] for row in result.evidence} == {"render_attempt", "capability_error"}
    state.execute.assert_not_awaited()
