"""Grounded context and validation for semantic command planning.

The model selects meanings/references; these functions retain server authority over
identity, channel boundaries, and frozen action arguments.
"""

from __future__ import annotations

import uuid
from typing import Any

from katcha.ai.command_planner import CommandPlan
from katcha.command_center_models import CommandActionProposal, CommandTurn
from katcha.control_contract import COMMAND_ACTION_SCOPES
from katcha.services.command_activity import get_action_activity


def planning_context(
    turns: list[CommandTurn],
    proposals: list[CommandActionProposal],
    explicit_clip_ids: list[uuid.UUID],
    resource_evidence: list[dict[str, object]],
) -> dict[str, Any]:
    return {
        "history": [
            {
                "id": str(turn.id),
                "role": turn.role,
                "content": turn.content[:4000],
                "intent": turn.intent,
                "evidence": list(turn.evidence or [])[:20],
                "context": dict(turn.turn_context or {}),
            }
            for turn in turns[-12:]
        ],
        "actions": [
            {
                "proposal_id": str(proposal.id),
                "source_turn_id": str(proposal.source_turn_id),
                "action_type": proposal.action_type,
                "label": proposal.label,
                "description": proposal.description,
                "status": proposal.status,
                "expires_at": str(proposal.expires_at),
                "payload": dict(proposal.payload or {}),
                "result": dict(proposal.result or {}),
                "error": proposal.error,
            }
            for proposal in proposals[-20:]
        ],
        "explicit_clip_ids": [str(value) for value in explicit_clip_ids],
        "attached_resources": resource_evidence,
        "action_scopes": COMMAND_ACTION_SCOPES,
    }


def resolve_planned_clip_ids(
    plan: CommandPlan,
    context: dict[str, Any],
) -> list[uuid.UUID]:
    allowed = set(context.get("explicit_clip_ids") or [])
    records = [*(context.get("attached_resources") or []), *(context.get("observations") or [])]
    for turn in context.get("history") or []:
        records.extend(turn.get("evidence") or [])
        turn_context = turn.get("context") or {}
        allowed.update(turn_context.get("resolved_selected_clip_ids") or [])
        allowed.update(turn_context.get("selected_clip_ids") or [])
    allowed.update(str(record.get("id")) for record in records if record.get("kind") == "clip")
    selected = list(dict.fromkeys(plan.selected_clip_ids))
    if any(str(value) not in allowed for value in selected):
        raise ValueError("AI selected a clip outside the grounded conversation context")
    return selected


def workflow_observations(
    proposals: list[CommandActionProposal],
    channel_profile_id: uuid.UUID,
) -> list[dict[str, Any]]:
    """Read fresh lifecycle feedback instead of treating startup as completion."""
    observations = []
    for proposal in proposals[-20:]:
        if proposal.channel_profile_id != channel_profile_id:
            raise ValueError("workflow feedback belongs to a different channel")
        if proposal.status != "executed":
            continue
        try:
            activity = get_action_activity(proposal.id, limit=5)
            if activity.proposal.channel_profile_id != channel_profile_id:
                raise ValueError("workflow feedback belongs to a different channel")
        except ValueError as exc:
            observations.append({"kind": "capability_error", "id": str(proposal.id),
                                 "status": "unavailable", "error": str(exc)})
            continue
        resource = activity.resource
        observations.append({
            "kind": "workflow_observation", "id": str(proposal.id),
            "workflow_id": activity.workflow_id, "state": activity.state,
            "settled": activity.settled,
            "detail": activity.workflow_detail,
            "resource": ({"kind": resource.kind, "id": str(resource.id),
                          "status": resource.status, "stage": resource.stage,
                          "error": resource.error} if resource else None),
            "events": list(activity.events),
        })
    return observations


def validate_bound_plan(initial: CommandPlan, bound: CommandPlan) -> None:
    """Observation can bind/reduce requested work, never expand authorization."""
    if set(bound.requested_actions) - set(initial.requested_actions):
        raise ValueError("Observation planning added an operation outside the original request")
    if initial.execution != "run" and bound.execution == "run":
        raise ValueError("Observation planning cannot turn a proposal into authorization")
    if not initial.recurring and bound.recurring:
        raise ValueError("Observation planning cannot turn a one-time request into recurring work")
    if not initial.prepare_for_production and bound.prepare_for_production:
        raise ValueError("Observation planning cannot add unrequested production preparation")


def resolve_planned_proposals(
    plan: CommandPlan,
    proposals: list[CommandActionProposal],
    channel_profile_id: uuid.UUID,
) -> list[CommandActionProposal]:
    if not plan.proposal_ids:
        raise ValueError("The action to proceed with was not resolved; choose an action card")
    by_id = {proposal.id: proposal for proposal in proposals}
    selected = []
    for proposal_id in dict.fromkeys(plan.proposal_ids):
        proposal = by_id.get(proposal_id)
        if proposal is None or proposal.channel_profile_id != channel_profile_id:
            raise ValueError("AI selected an action outside this channel conversation")
        if proposal.status not in {"proposed", "failed", "executing", "executed"}:
            raise ValueError("The selected action has expired or is unavailable; prepare it again")
        if proposal.action_type not in COMMAND_ACTION_SCOPES:
            raise ValueError("The selected action is no longer supported")
        selected.append(proposal)
    return selected
