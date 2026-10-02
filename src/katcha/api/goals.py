"""Durable natural-language requests with restart-safe client identities."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, HTTPException, Request
from temporalio.common import WorkflowIDReusePolicy
from temporalio.exceptions import WorkflowAlreadyStartedError

from katcha.api.command_center import CommandRequest
from katcha.api.control_auth import (
    control_actor,
    control_scopes,
    require_control_channel,
    require_control_scope,
)
from katcha.db import session_scope
from katcha.goal_models import CommandGoal
from katcha.intelligence.runtime import INTELLIGENCE_TASK_QUEUE
from katcha.orchestration.client import get_temporal_client
from katcha.orchestration.goal_workflows import CommandGoalWorkflow
from katcha.services.goal_receipts import TERMINAL_GOAL_STATES, get_goal, register_goal

router = APIRouter(prefix="/v1/ai/goals", tags=["command goals"])


class GoalRequest(CommandRequest):
    command_id: uuid.UUID


def snapshot(goal: CommandGoal) -> dict:
    return {
        "goal_id": str(goal.id),
        "command_id": str(goal.command_id),
        "thread_id": str(goal.thread_id),
        "channel_profile_id": str(goal.channel_profile_id),
        "status": goal.status,
        "summary": goal.summary,
        "step_count": goal.step_count,
        "deadline": goal.deadline.isoformat(),
        "result": goal.result,
        "error": goal.error,
    }


def accessible_goal(request: Request, goal_id: uuid.UUID) -> CommandGoal:
    require_control_scope(request, "ai:read")
    try:
        goal = get_goal(goal_id)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc
    require_control_channel(request, goal.channel_profile_id)
    if control_actor(request) != goal.actor:
        raise HTTPException(403, "This saved request belongs to a different control actor")
    return goal


@router.post("", status_code=202)
async def submit_goal(http_request: Request, request: GoalRequest) -> dict:
    require_control_scope(http_request, "ai:read")
    require_control_scope(http_request, "ai:command")
    require_control_channel(http_request, request.channel_profile_id)
    authority = {
        "actor": control_actor(http_request),
        "scopes": sorted(control_scopes(http_request)),
        "channel_ids": sorted(http_request.state.control_channel_profile_ids),
        "principal_name": http_request.state.control_principal_name,
        "credential_id": http_request.state.control_credential_id,
        "credential_fingerprint": http_request.state.control_credential_fingerprint,
    }
    try:
        goal = register_goal(
            request.command_id, request.model_dump(mode="json", exclude={"command_id"}), authority
        )
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    if goal.status not in TERMINAL_GOAL_STATES:
        try:
            client = await get_temporal_client()
            await client.start_workflow(
                CommandGoalWorkflow.run,
                str(goal.id),
                id=f"command-goal-{goal.id}",
                task_queue=INTELLIGENCE_TASK_QUEUE,
                id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
            )
        except WorkflowAlreadyStartedError:
            pass
        except Exception:
            # The receipt already exists. Retry the identical command_id to dispatch safely.
            response = snapshot(get_goal(goal.id))
            response["dispatch_pending"] = True
            response["summary"] = "Request saved. Reconnect and retry to start it."
            return response
    return snapshot(get_goal(goal.id))


@router.get("/{goal_id}")
def read_goal(http_request: Request, goal_id: uuid.UUID) -> dict:
    return snapshot(accessible_goal(http_request, goal_id))


@router.post("/{goal_id}/cancel")
def cancel_goal(http_request: Request, goal_id: uuid.UUID) -> dict:
    goal = accessible_goal(http_request, goal_id)
    require_control_scope(http_request, "ai:write")
    with session_scope() as session:
        current = session.get(CommandGoal, goal.id)
        if current.status not in TERMINAL_GOAL_STATES:
            current.status = "cancelled"
            current.summary = "Planning stopped. Work already started retains its own status."
    return snapshot(get_goal(goal.id))
