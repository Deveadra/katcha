"""Durable natural-language requests with restart-safe client identities."""

from __future__ import annotations

import asyncio
import logging
import uuid

from fastapi import APIRouter, HTTPException, Request
from sqlalchemy import select
from temporalio.client import WorkflowExecutionStatus
from temporalio.common import WorkflowIDReusePolicy
from temporalio.exceptions import WorkflowAlreadyStartedError

from katcha.api.command_center import CommandRequest
from katcha.api.control_auth import (
    control_actor,
    control_scopes,
    require_control_channel,
    require_control_scope,
)
from katcha.command_center_models import CommandActionProposal
from katcha.db import session_scope
from katcha.goal_models import CommandGoal, CommandGoalStep
from katcha.intelligence.runtime import INTELLIGENCE_TASK_QUEUE
from katcha.orchestration.client import get_temporal_client
from katcha.orchestration.failure_details import failure_details, safe_failure_text
from katcha.orchestration.goal_workflows import CommandGoalWorkflow
from katcha.services.goal_receipts import TERMINAL_GOAL_STATES, get_goal, register_goal

router = APIRouter(prefix="/v1/ai/goals", tags=["command goals"])
logger = logging.getLogger(__name__)


class GoalRequest(CommandRequest):
    command_id: uuid.UUID


def retryable(goal: CommandGoal) -> bool:
    """Retry only receipts that stopped before any decision or action was saved."""
    if (
        goal.status not in {"blocked", "failed"}
        or goal.step_count != 0
        or goal.observations != []
        or goal.authorization != {}
    ):
        return False
    with session_scope() as session:
        step = session.scalar(select(CommandGoalStep.id).where(CommandGoalStep.goal_id == goal.id))
        proposal = session.scalar(
            select(CommandActionProposal.id).where(
                CommandActionProposal.request_id == goal.command_id
            )
        )
        return step is None and proposal is None


def workflow_diagnostic(
    goal: CommandGoal, *, code: str, stage: str, error_type: str | None = None
) -> dict:
    """Stable, actor-scoped workflow diagnostics without exposing transport credentials."""
    return {
        "code": code,
        "stage": stage,
        "workflow_id": f"command-goal-{goal.id}",
        "error_type": error_type,
        "next_action": (
            "The request is saved. Retry the same request identity after checking "
            "Temporal connectivity; do not create a new request."
            if stage == "workflow_dispatch"
            else "Check Temporal service and worker health; retain the saved goal ID."
        ),
    }


def snapshot(goal: CommandGoal) -> dict:
    summary, result = goal.summary, goal.result
    if goal.status == "blocked":
        from katcha.services.goal_runner import _saved_draft_summary

        saved_draft = _saved_draft_summary(goal)
        if saved_draft and not (summary or "").startswith(saved_draft):
            summary = saved_draft + (summary or "")
            result = {**(result or {}), "answer": saved_draft + (result or {}).get("answer", "")}
    response = {
        "goal_id": str(goal.id),
        "command_id": str(goal.command_id),
        "thread_id": str(goal.thread_id),
        "channel_profile_id": str(goal.channel_profile_id),
        "request": goal.request,
        "status": goal.status,
        "summary": summary,
        "step_count": goal.step_count,
        "deadline": goal.deadline.isoformat(),
        "result": result,
        "error": goal.error,
        "retryable": retryable(goal),
    }
    if goal.status == "queued" and (goal.error or "").startswith("goal_dispatch_unavailable"):
        response["dispatch_pending"] = True
        response["diagnostic"] = workflow_diagnostic(
            goal, code="goal_dispatch_unavailable", stage="workflow_dispatch"
        )
    elif goal.status in {"blocked", "failed"}:
        response["diagnostic"] = {
            "code": f"goal_{goal.status}",
            "stage": "goal_execution",
            "workflow_id": f"command-goal-{goal.id}",
            "message": safe_failure_text(goal.error) if goal.error else None,
        }
    return response


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
        except Exception as exc:
            # No new receipt or replay: the same command identity is safe to retry.
            # Persist only a stable code/type; exception strings may contain credentials.
            logger.warning(
                "Goal workflow dispatch unconfirmed goal_id=%s error_type=%s",
                goal.id, type(exc).__name__,
            )
            with session_scope() as session:
                current = session.get(CommandGoal, goal.id)
                if current.status == "queued":
                    current.error = f"goal_dispatch_unavailable ({type(exc).__name__})"
            response = snapshot(get_goal(goal.id))
            response["dispatch_pending"] = True
            response["diagnostic"] = workflow_diagnostic(
                goal, code="goal_dispatch_unavailable",
                stage="workflow_dispatch", error_type=type(exc).__name__,
            )
            response["summary"] = (
                "Request saved, but Temporal could not confirm workflow start. "
                "Katcha can retry using the same request identity."
            )
            return response
        with session_scope() as session:
            current = session.get(CommandGoal, goal.id)
            if current.status == "queued":
                current.status = "running"
                current.error = None
                current.summary = "Request saved; deciding the first step."
    return snapshot(get_goal(goal.id))


@router.post("/{goal_id}/retry", status_code=202)
async def retry_goal(http_request: Request, goal_id: uuid.UUID) -> dict:
    goal = accessible_goal(http_request, goal_id)
    if not retryable(goal):
        raise HTTPException(409, "This request has saved work and cannot be restarted safely")
    request = GoalRequest.model_validate(
        {
            **goal.request,
            "thread_id": str(goal.thread_id),
            "command_id": str(uuid.uuid5(goal.id, "retry")),
        }
    )
    return await submit_goal(http_request, request)


@router.get("")
def list_goals(http_request: Request, channel_profile_id: uuid.UUID) -> list[dict]:
    require_control_scope(http_request, "ai:read")
    require_control_channel(http_request, channel_profile_id)
    with session_scope() as session:
        rows = session.scalars(
            select(CommandGoal)
            .where(
                CommandGoal.channel_profile_id == channel_profile_id,
                CommandGoal.actor == control_actor(http_request),
            )
            .order_by(CommandGoal.created_at.desc())
            .limit(100)
        )
        return [snapshot(row) for row in rows]


@router.get("/{goal_id}")
async def read_goal(http_request: Request, goal_id: uuid.UUID) -> dict:
    goal = accessible_goal(http_request, goal_id)
    if goal.status not in TERMINAL_GOAL_STATES:

        async def describe():
            client = await get_temporal_client()
            return await client.get_workflow_handle(f"command-goal-{goal.id}").describe()

        try:
            description = await asyncio.wait_for(describe(), timeout=5)
        except Exception as exc:
            # A missing/unreachable Temporal status is NOT proof of completion,
            # cancellation or a revoked identity. Never silently show "working".
            logger.warning(
                "Goal workflow status unverified goal_id=%s error_type=%s",
                goal.id, type(exc).__name__,
            )
            response = snapshot(goal)
            response["workflow_status_unverified"] = True
            response["diagnostic"] = workflow_diagnostic(
                goal,
                code=(
                    "goal_dispatch_unavailable"
                    if response.get("dispatch_pending")
                    else "goal_workflow_status_unverified"
                ),
                stage=(
                    "workflow_dispatch" if response.get("dispatch_pending")
                    else "workflow_status"
                ),
                error_type=type(exc).__name__,
            )
            return response
        if description.status in {
            WorkflowExecutionStatus.FAILED,
            WorkflowExecutionStatus.TIMED_OUT,
            WorkflowExecutionStatus.TERMINATED,
            WorkflowExecutionStatus.CANCELED,
        }:
            from katcha.services.goal_runner import _finish

            error = description.status.name
            if description.status == WorkflowExecutionStatus.FAILED:
                try:
                    async def failed_result():
                        client = await get_temporal_client()
                        handle = client.get_workflow_handle(f"command-goal-{goal.id}")
                        # Older Temporal fixtures and limited handles expose only
                        # execution status. Lack of details must not replace FAILED
                        # with an incidental AttributeError.
                        if not callable(getattr(handle, "result", None)):
                            return None
                        return await handle.result()

                    await asyncio.wait_for(failed_result(), timeout=3)
                except Exception as exc:
                    # Actual child cause instead of a generic Temporal wrapper.
                    error = failure_details(exc)["message"]
            _finish(
                goal.id,
                "failed",
                "The goal runner stopped. Its saved work remains available.",
                error,
            )
            goal = get_goal(goal.id)
    return snapshot(goal)


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


@router.get("/{goal_id}/browser")
async def browser_snapshot(http_request: Request, goal_id: uuid.UUID) -> dict:
    from katcha.services.browser_client import run_browser

    goal = accessible_goal(http_request, goal_id)
    return await run_browser(goal, {"action": "snapshot"})


@router.post("/{goal_id}/browser")
async def browser_interact(http_request: Request, goal_id: uuid.UUID) -> dict:
    from pydantic import ValidationError

    from katcha.services.browser_client import run_browser
    from katcha.services.server_browser import BrowserAction

    goal = accessible_goal(http_request, goal_id)
    require_control_scope(http_request, "ai:write")
    # Read a bounded body. Direct operator form entries never become goal observations.
    body = bytearray()
    async for chunk in http_request.stream():
        body.extend(chunk)
        if len(body) > 20000:
            raise HTTPException(413, "Browser action is too large")
    try:
        action = BrowserAction.model_validate_json(body)
    except ValidationError:
        # Do not echo rejected passwords or form values through validation errors.
        raise HTTPException(422, "Choose an action and provide its required fields") from None
    if goal.status in {"cancelled", "completed"} and action.action != "close":
        raise HTTPException(409, "This request has stopped. Open a new request to browse.")
    if action.action == "screenshot":
        raise HTTPException(422, "Use the browser screenshot link")
    return await run_browser(goal, action.model_dump())


@router.get("/{goal_id}/browser/screenshot")
async def browser_screenshot(
    http_request: Request, goal_id: uuid.UUID, snapshot_id: str | None = None,
):
    import base64

    from fastapi.responses import Response

    from katcha.services.browser_client import run_browser

    goal = accessible_goal(http_request, goal_id)
    result = await run_browser(goal, {"action": "screenshot", "snapshot_id": snapshot_id})
    if not result.get("image"):
        raise HTTPException(409, result.get("message", "The browser page is unavailable"))
    return Response(base64.b64decode(result["image"]), media_type="image/png",
                    headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})
