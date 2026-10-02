"""Persist decisions before native execution, then re-plan from real observations."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from starlette.concurrency import run_in_threadpool
from temporalio.client import WorkflowExecutionStatus

from katcha.ai.goal_planner import GoalDecision, decide_goal
from katcha.command_center_models import CommandActionProposal
from katcha.db import session_scope
from katcha.goal_models import CommandGoal, CommandGoalStep
from katcha.services.command_environment import command_environment
from katcha.services.command_history import (
    list_command_turns,
    list_thread_proposals,
    record_command_exchange,
)
from katcha.services.goal_receipts import (
    MAX_GOAL_STEPS,
    TERMINAL_GOAL_STATES,
    get_goal,
    resolve_goal_authority,
    utc,
)
from katcha.services.goal_tools import (
    TOOLS,
    _redact,
    action_spec,
    run_native_tool,
    run_read_tool,
    tool_catalog,
)


def _step(goal_id, number):
    with session_scope() as session:
        row = session.scalar(
            select(CommandGoalStep).where(
                CommandGoalStep.goal_id == goal_id,
                CommandGoalStep.number == number,
            )
        )
        if row:
            session.expunge(row)
        return row


def _save_result(goal_id, step_id, result, error=None):
    with session_scope() as session:
        goal = session.get(CommandGoal, goal_id)
        step = session.get(CommandGoalStep, step_id)
        step.result, step.error, step.status = result, error, "observed"
        if goal.status == "cancelled":
            return
        goal.observations = [
            *goal.observations,
            {
                "step": step.number,
                "tool": step.decision.get("tool"),
                "arguments": step.decision.get("arguments", {}),
                "result": result,
                "error": error,
            },
        ]
        goal.step_count = step.number + 1
        goal.status = "running"
        goal.summary = "Result saved; deciding the next step."


def _finish(goal_id, state, answer, error=None):
    with session_scope() as session:
        goal = session.scalar(
            select(CommandGoal).where(CommandGoal.id == goal_id).with_for_update()
        )
        if goal.status in TERMINAL_GOAL_STATES:
            return
        goal.status, goal.summary, goal.error = state, answer[:6000], error
        evidence = []
        for observation in goal.observations:
            result = observation.get("result") or {}
            evidence.extend(result.get("evidence", []) if isinstance(result, dict) else [])
            evidence.append(
                {
                    "kind": "goal_step",
                    "id": str(observation["step"]),
                    "tool": observation.get("tool"),
                    "error": observation.get("error"),
                    "result": result,
                }
            )
        goal.result = {
            "answer": answer[:6000],
            "thread_id": str(goal.thread_id),
            "request_id": str(goal.command_id),
            "intent": "goal_" + state,
            "narrator": "Katcha goal runner",
            "evidence": evidence[-30:],
            "actions": [],
        }
        thread_id, command_id, prompt, result = (
            goal.thread_id,
            goal.command_id,
            goal.request["prompt"],
            dict(goal.result),
        )
    # A terminal receipt is durable even if conversation finalization is temporarily unavailable.
    record_command_exchange(
        thread_id=thread_id,
        request_id=command_id,
        user_content=prompt,
        assistant_content=answer[:6000],
        intent="goal_" + state,
        narrator="Katcha goal runner",
        evidence=result["evidence"],
        user_context={},
        assistant_context={"goal_id": str(goal_id), "status": state},
    )


def _workflow_id(result):
    if not isinstance(result, dict):
        return None
    return result.get("workflow_id") or _workflow_id(result.get("result"))


async def _observe_background(goal, step):
    workflow_id = _workflow_id(step.result)
    if not workflow_id:
        _save_result(goal.id, step.id, step.result, step.error)
        return
    from katcha.orchestration.client import get_temporal_client

    try:
        client = await get_temporal_client()
        handle = client.get_workflow_handle(str(workflow_id))
        description = await handle.describe()
    except Exception:
        with session_scope() as session:
            current = session.get(CommandGoal, goal.id)
            if current.status not in TERMINAL_GOAL_STATES:
                current.status = "waiting_workflow"
                current.summary = "Waiting to reconnect to saved work; its result is retained."
        return
    if description.status == WorkflowExecutionStatus.RUNNING:
        if step.decision.get("tool") == "schedule_watch":
            _save_result(
                goal.id,
                step.id,
                {**step.result, "workflow_state": "RUNNING", "ongoing_collection_started": True},
            )
            return
        with session_scope() as session:
            current = session.get(CommandGoal, goal.id)
            if current.status != "cancelled":
                current.status, current.summary = (
                    "waiting_workflow",
                    "Work is running; I will continue when its result arrives.",
                )
        return
    if (
        step.decision.get("tool") == "cancel_workflow"
        and description.status == WorkflowExecutionStatus.CANCELED
    ):
        _save_result(goal.id, step.id, {**step.result, "workflow_state": "CANCELED"})
        return
    try:
        result = await handle.result()
        from katcha.services.command_activity import get_action_activity

        activity = get_action_activity(step.proposal_id) if step.proposal_id else None
        error = None
        if isinstance(result, dict) and (
            result.get("status") in {"failed", "blocked"}
            or result.get("success") is False
            or result.get("error")
        ):
            error = str(result.get("error") or "Background work reported an unsuccessful result")
        if activity and activity.state == "failed":
            error = str(activity.workflow_detail.get("error") or "Background preparation failed")
        _save_result(
            goal.id,
            step.id,
            {
                **step.result,
                "workflow_state": description.status.name,
                "workflow_result": _redact(result),
                "lifecycle_state": activity.state if activity else None,
            },
            error,
        )
    except Exception as exc:
        _save_result(
            goal.id,
            step.id,
            {**step.result, "workflow_state": description.status.name},
            f"Background work did not complete: {str(exc)[:2000]}",
        )


async def execute_native_goal_proposal(proposal, actor: str) -> dict:
    payload = proposal.payload
    goal = get_goal(uuid.UUID(payload["goal_id"]))
    scopes, _ = resolve_goal_authority(goal)
    if actor != goal.actor or ("*" not in scopes and "ai:write" not in scopes):
        raise ValueError("This actor cannot authorize the saved goal")
    with session_scope() as session:
        step = session.get(CommandGoalStep, uuid.UUID(payload["step_id"]))
        if not step or step.goal_id != goal.id or step.proposal_id != proposal.id:
            raise ValueError("The proposal is not the goal's exact frozen step")
        if (
            payload["tool"] != step.decision["tool"]
            or payload["arguments"] != step.decision["arguments"]
        ):
            raise ValueError("The proposal arguments differ from the frozen goal decision")
        if goal.status in TERMINAL_GOAL_STATES:
            raise ValueError("This goal has stopped; no new operation can start")
        tool = TOOLS[payload["tool"]]
        if "*" not in scopes and tool.scope not in scopes:
            raise ValueError("The native operation permission was revoked")
        if step.status in {"observed", "waiting_workflow"}:
            return dict(step.result)
        if step.status == "executing" and not tool.retry_safe:
            raise ValueError(
                "This operation has an uncertain result; inspect current state before retrying"
            )
        step.status = "executing"
    result = await run_native_tool(goal, payload["tool"], payload["arguments"], step.id)
    with session_scope() as session:
        step = session.get(CommandGoalStep, step.id)
        step.result, step.status = result, "waiting_workflow"
    return result


def _proposal_for_step(goal, step):
    spec = action_spec(goal, step.decision["tool"], step.decision["arguments"], step.id)
    with session_scope() as session:
        current = session.scalar(
            select(CommandGoalStep).where(CommandGoalStep.id == step.id).with_for_update()
        )
        if current.proposal_id:
            proposal = session.get(CommandActionProposal, current.proposal_id)
        else:
            proposal = CommandActionProposal(
                id=uuid.uuid5(step.id, "proposal"),
                request_id=goal.command_id,
                thread_id=goal.thread_id,
                channel_profile_id=goal.channel_profile_id,
                action_type=spec.action_type,
                label=spec.label,
                description=spec.description,
                payload=spec.payload,
                status="proposed",
                idempotency_key=f"goal-step:{step.id}",
                result={},
                expires_at=utc(goal.deadline),
                execution_attempts=0,
            )
            session.add(proposal)
            current.proposal_id = proposal.id
        session.flush()
        session.expunge(proposal)
        return proposal


async def advance_goal(goal_id: uuid.UUID) -> str:
    """One durable step. Activity retries reuse its stored decision and proposal."""
    goal = get_goal(goal_id)
    if goal.status in TERMINAL_GOAL_STATES:
        return goal.status
    try:
        scopes, _ = resolve_goal_authority(goal)
        if utc(goal.deadline) <= datetime.now(UTC):
            _finish(
                goal.id,
                "blocked",
                "This goal reached its time limit. Completed work is saved; no new work was "
                "started.",
            )
            return "blocked"
        if goal.step_count >= MAX_GOAL_STEPS:
            _finish(
                goal.id,
                "blocked",
                "This goal reached its planning limit. Its observations and completed work "
                "are saved.",
            )
            return "blocked"
        step = _step(goal.id, goal.step_count)
        if step is None:
            history = list_command_turns(goal.thread_id)[-12:]
            proposals = list_thread_proposals(goal.thread_id)[-20:]
            context = {
                "original_request": goal.request,
                "environment": command_environment(goal.channel_profile_id),
                "history": [
                    {
                        "role": turn.role,
                        "content": turn.content[:4000],
                        "evidence": turn.evidence[:10],
                    }
                    for turn in history
                ],
                "prior_actions": [
                    {
                        "proposal_id": str(p.id),
                        "label": p.label,
                        "status": p.status,
                        "payload": _redact(p.payload),
                        "result": _redact(p.result),
                    }
                    for p in proposals
                ],
                "tools": tool_catalog(scopes),
                "authorization": goal.authorization,
                "step": goal.step_count,
                "max_steps": MAX_GOAL_STEPS,
                "observations": goal.observations[-16:],
            }
            decision = await run_in_threadpool(
                decide_goal,
                goal_id=goal.id,
                channel_id=goal.channel_profile_id,
                number=goal.step_count,
                context=context,
            )
            with session_scope() as session:
                current = session.scalar(
                    select(CommandGoal).where(CommandGoal.id == goal.id).with_for_update()
                )
                if current.status in TERMINAL_GOAL_STATES:
                    return current.status
                if not current.authorization:
                    if any(
                        name not in TOOLS or not TOOLS[name].mutates
                        for name in decision.allowed_mutations
                    ):
                        raise ValueError("The planner selected an unregistered mutation")
                    current.authorization = {
                        "mode": decision.mode,
                        "allowed_mutations": decision.allowed_mutations,
                        "completion_criteria": decision.completion_criteria,
                    }
                step = CommandGoalStep(
                    id=uuid.uuid5(goal.id, f"step:{goal.step_count}"),
                    goal_id=goal.id,
                    number=goal.step_count,
                    decision=decision.model_dump(mode="json"),
                    status="planned",
                    result={},
                )
                session.add(step)
                current.status, current.summary = "running", decision.reason
                session.flush()
                session.expunge(step)
            goal = get_goal(goal.id)
        decision = GoalDecision.model_validate(step.decision)
        if step.status == "waiting_workflow":
            await _observe_background(goal, step)
            return get_goal(goal.id).status
        if decision.outcome != "tool":
            if (
                decision.outcome == "complete"
                and goal.authorization.get("mode") == "run"
                and goal.authorization.get("allowed_mutations")
                and not any(
                    TOOLS.get(o.get("tool"))
                    and TOOLS[o["tool"]].mutates
                    and not o.get("error")
                    and o.get("result")
                    for o in goal.observations
                )
            ):
                raise ValueError("The requested operation has no observed successful result")
            state = {"complete": "completed", "clarify": "needs_input", "blocked": "blocked"}[
                decision.outcome
            ]
            _finish(goal.id, state, decision.answer or decision.reason)
            return state
        tool = TOOLS.get(decision.tool)
        if tool is None:
            _save_result(goal.id, step.id, {}, "The chosen capability is not registered")
            return "running"
        if "*" not in scopes and tool.scope not in scopes:
            raise ValueError(f"This request requires {tool.scope} permission")
        if not tool.mutates:
            try:
                result = await run_read_tool(goal, tool.name, decision.arguments)
                _save_result(goal.id, step.id, result)
            except (KeyError, TypeError, ValueError) as exc:
                _save_result(goal.id, step.id, {}, str(exc))
            return "running"
        for previous in goal.observations:
            if (
                previous.get("tool") == tool.name
                and previous.get("arguments") == decision.arguments
                and (
                    previous.get("result", {}).get("uncertain")
                    or (not previous.get("error") and previous.get("result"))
                )
            ):
                raise ValueError(
                    "This exact operation already ran or has an uncertain result; "
                    "inspect its state before requesting new work"
                )
        authorization = goal.authorization
        if (
            tool.name not in authorization["allowed_mutations"]
            or authorization["mode"] == "inspect"
        ):
            raise ValueError("This operation is outside the operator's frozen goal authorization")
        if "*" not in scopes and "ai:write" not in scopes:
            raise ValueError("This request requires ai:write permission")
        try:
            proposal = _proposal_for_step(goal, step)
        except (KeyError, TypeError, ValueError) as exc:
            _save_result(goal.id, step.id, {}, str(exc))
            return "running"
        from katcha.api.command_center import _action_response, _run_frozen_command_action

        if proposal.status in {"proposed", "failed"} and (
            tool.confirm or authorization["mode"] == "propose"
        ):
            with session_scope() as session:
                current = session.get(CommandGoal, goal.id)
                current.status = "waiting_confirmation"
                current.summary = "The exact operation is ready for your review and confirmation."
                current.result = {
                    "answer": current.summary,
                    "thread_id": str(goal.thread_id),
                    "intent": "goal_proposal",
                    "evidence": [],
                    "actions": [_action_response(proposal).model_dump(mode="json")],
                }
            return "waiting_confirmation"
        if proposal.status == "executing":
            if proposal.execution_started_at is not None and utc(
                proposal.execution_started_at
            ) > datetime.now(UTC) - timedelta(minutes=5):
                with session_scope() as session:
                    current = session.get(CommandGoal, goal.id)
                    if current.status != "cancelled":
                        current.status = "waiting_workflow"
                        current.summary = "Waiting for the saved operation's execution result."
                return "waiting_workflow"
            if not tool.retry_safe:
                _save_result(
                    goal.id,
                    step.id,
                    {"uncertain": True},
                    "The operation's result is uncertain; inspect current state",
                )
                return "running"
        if proposal.status != "executed":
            try:
                proposal, _ = await _run_frozen_command_action(
                    proposal,
                    actor=goal.actor,
                    credential_id=goal.authority.get("credential_id"),
                    credential_fingerprint=goal.authority.get("credential_fingerprint"),
                )
            except Exception as exc:
                _save_result(goal.id, step.id, {"uncertain": not tool.retry_safe}, str(exc))
                return "running"
        if proposal.status != "executed":
            _save_result(goal.id, step.id, {}, proposal.error or "The operation was not accepted")
            return "running"
        with session_scope() as session:
            current = session.get(CommandGoalStep, step.id)
            current.status, current.result = "waiting_workflow", dict(proposal.result)
        await _observe_background(goal, _step(goal.id, goal.step_count))
        return get_goal(goal.id).status
    except ValueError as exc:
        # Read and validation failures become observations for argument repair.
        step = _step(goal.id, goal.step_count)
        if (
            step
            and step.decision.get("outcome") == "tool"
            and not TOOLS.get(step.decision.get("tool"), None)
        ):
            _save_result(goal.id, step.id, {}, str(exc))
            return "running"
        _finish(goal.id, "blocked", str(exc), str(exc))
        return "blocked"
    except Exception as exc:
        _finish(
            goal.id, "failed", f"This saved request stopped: {str(exc)[:2000]}", str(exc)[:2000]
        )
        return "failed"


def validate_goal_proposal(proposal, actor: str) -> None:
    """All goal actions, including legacy production actions, obey the saved authority."""
    if not str(getattr(proposal, "idempotency_key", "")).startswith("goal-step:"):
        return
    with session_scope() as session:
        step = session.scalar(
            select(CommandGoalStep).where(CommandGoalStep.proposal_id == proposal.id)
        )
        if step is None:
            return
        goal_id, tool_name = step.goal_id, step.decision["tool"]
    goal = get_goal(goal_id)
    scopes, _ = resolve_goal_authority(goal)
    if actor != goal.actor:
        raise ValueError("This saved goal belongs to a different actor")
    if goal.status in TERMINAL_GOAL_STATES or utc(goal.deadline) <= datetime.now(UTC):
        raise ValueError("This saved goal has stopped or expired")
    if "*" not in scopes and not {"ai:write", TOOLS[tool_name].scope} <= scopes:
        raise ValueError("The saved goal operation permission was revoked")
