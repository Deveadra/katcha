from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, HTTPException, Query, Request, status
from pydantic import BaseModel, Field

from katcha.ai.command_center import compose_grounded_answer
from katcha.api.control_auth import control_actor, require_control_scope
from katcha.command_center_models import (
    CommandActionProposal,
    CommandThread,
    CommandTurn,
)
from katcha.db import session_scope
from katcha.domain import ProductionStatus
from katcha.orchestration.client import (
    start_channel_intelligence_refresh,
    start_production_workflow,
    start_short_episode_editorial_workflow,
)
from katcha.production_models import Production
from katcha.services.command_actions import (
    ActionProposalSpec,
    claim_action_proposal,
    complete_action_proposal,
    create_action_proposals,
    fail_action_proposal,
    get_action_proposal,
)
from katcha.services.command_activity import get_action_activity
from katcha.services.command_center import (
    best_clips,
    build_short_episode_candidates,
    channel_status,
    classify_intent,
    clip_explanation,
    failures,
    infer_edit_blueprint_key,
    performance_advice,
    ranked_episode_allowed_counts,
    resolve_command_follow_up,
)
from katcha.services.intelligence_runs import (
    fail_channel_intelligence_run,
    start_channel_intelligence_run,
)
from katcha.services.command_history import (
    archive_command_thread,
    create_command_thread,
    get_command_thread,
    list_command_threads,
    list_command_turns,
    list_thread_proposals,
    record_command_exchange,
)
from katcha.services.productions import (
    register_regeneration,
    register_short_production,
)
from katcha.services.render_recovery import render_attempts_for_source
from katcha.services.short_episodes import register_short_episode

router = APIRouter(prefix="/v1/ai", tags=["katcha-ai"])

ActionType = Literal[
    "refresh_channel_intelligence",
    "create_short_production",
    "create_ranked_short_episode",
    "recover_production_render",
]

_ACTION_SCOPES: dict[str, str] = {
    "refresh_channel_intelligence": "intelligence:write",
    "create_short_production": "production:create",
    "create_ranked_short_episode": "production:create",
    "recover_production_render": "render:recover",
}


class CommandRequest(BaseModel):
    channel_profile_id: uuid.UUID
    thread_id: uuid.UUID | None = None
    prompt: str = Field(min_length=1, max_length=4000)
    selected_clip_ids: list[uuid.UUID] = Field(default_factory=list, max_length=20)
    selected_production_id: uuid.UUID | None = None


class CommandAction(BaseModel):
    proposal_id: uuid.UUID
    thread_id: uuid.UUID | None = None
    source_turn_id: uuid.UUID | None = None
    type: ActionType
    label: str
    description: str
    status: str
    expires_at: datetime
    payload: dict[str, object] = Field(default_factory=dict)
    requires_confirmation: bool = True


class ResolvedContextResponse(BaseModel):
    selected_clip_ids: list[uuid.UUID] = Field(default_factory=list)
    inherited_from_thread: bool = False
    source_turn_id: uuid.UUID | None = None
    resolution: str | None = None
    action_source_turn_id: uuid.UUID | None = None


class CommandResponse(BaseModel):
    request_id: uuid.UUID
    thread_id: uuid.UUID
    user_turn_id: uuid.UUID
    assistant_turn_id: uuid.UUID
    channel_profile_id: uuid.UUID
    intent: str
    answer: str
    key_points: list[str] = Field(default_factory=list)
    caveats: list[str] = Field(default_factory=list)
    evidence: list[dict[str, object]]
    actions: list[CommandAction]
    resolved_context: ResolvedContextResponse = Field(
        default_factory=ResolvedContextResponse
    )
    grounded: bool = True
    narrator: str


class ExecuteActionRequest(BaseModel):
    confirmed: bool


class ExecuteActionResponse(BaseModel):
    proposal_id: uuid.UUID
    action_type: str
    status: str
    execution_attempts: int
    result: dict[str, object] = Field(default_factory=dict)
    error: str | None = None


class ActionResourceActivityResponse(BaseModel):
    kind: str
    id: uuid.UUID
    workflow_id: str
    status: str
    stage: str
    generation: int
    error: str | None = None
    updated_at: datetime | None = None


class ActionActivityEventResponse(BaseModel):
    id: uuid.UUID
    event_type: str
    aggregate_type: str
    aggregate_id: str
    created_at: datetime


class ActionActivityResponse(BaseModel):
    proposal_id: uuid.UUID
    action_type: str
    proposal_status: str
    workflow_id: str | None = None
    state: str
    settled: bool
    resource: ActionResourceActivityResponse | None = None
    events: list[ActionActivityEventResponse] = Field(default_factory=list)


class ActionProposalStatusResponse(BaseModel):
    proposal_id: uuid.UUID
    request_id: uuid.UUID
    thread_id: uuid.UUID | None = None
    source_turn_id: uuid.UUID | None = None
    channel_profile_id: uuid.UUID
    action_type: str
    label: str
    description: str
    status: str
    execution_attempts: int
    expires_at: datetime
    confirmed_by: str | None = None
    confirmed_at: datetime | None = None
    execution_started_at: datetime | None = None
    executed_at: datetime | None = None
    result: dict[str, object] = Field(default_factory=dict)
    error: str | None = None
    payload: dict[str, object] = Field(default_factory=dict)


class ThreadSummaryResponse(BaseModel):
    thread_id: uuid.UUID
    channel_profile_id: uuid.UUID
    title: str
    status: str
    created_by: str
    last_activity_at: datetime
    archived_at: datetime | None = None
    created_at: datetime


class TurnResponse(BaseModel):
    turn_id: uuid.UUID
    thread_id: uuid.UUID
    channel_profile_id: uuid.UUID
    sequence_number: int
    role: str
    request_id: uuid.UUID | None = None
    intent: str | None = None
    narrator: str | None = None
    content: str
    evidence: list[dict[str, object]] = Field(default_factory=list)
    context: dict[str, object] = Field(default_factory=dict)
    created_at: datetime


class ThreadDetailResponse(BaseModel):
    thread: ThreadSummaryResponse
    turns: list[TurnResponse]
    actions: list[ActionProposalStatusResponse]


def _thread_summary(thread: CommandThread) -> ThreadSummaryResponse:
    return ThreadSummaryResponse(
        thread_id=thread.id,
        channel_profile_id=thread.channel_profile_id,
        title=thread.title,
        status=thread.status,
        created_by=thread.created_by,
        last_activity_at=thread.last_activity_at,
        archived_at=thread.archived_at,
        created_at=thread.created_at,
    )


def _turn_response(turn: CommandTurn) -> TurnResponse:
    return TurnResponse(
        turn_id=turn.id,
        thread_id=turn.thread_id,
        channel_profile_id=turn.channel_profile_id,
        sequence_number=turn.sequence_number,
        role=turn.role,
        request_id=turn.request_id,
        intent=turn.intent,
        narrator=turn.narrator,
        content=turn.content,
        evidence=list(turn.evidence or []),
        context=dict(turn.turn_context or {}),
        created_at=turn.created_at,
    )


def _uuid_from_prompt(prompt: str) -> uuid.UUID | None:
    for token in prompt.replace(",", " ").replace(".", " ").split():
        try:
            return uuid.UUID(token.strip("()[]{}"))
        except ValueError:
            continue
    return None


def _action_specs(
    request: CommandRequest,
    intent: str,
    evidence: list[dict[str, object]],
) -> list[ActionProposalSpec]:
    specs: list[ActionProposalSpec] = []
    blueprint_key = infer_edit_blueprint_key(request.prompt)

    if intent == "failures":
        for item in evidence:
            if (
                item.get("kind") == "render_attempt"
                and item.get("source_type") == "production"
                and item.get("status") == "dead_letter"
                and item.get("production_id")
            ):
                production_id = str(item["production_id"])
                specs.append(
                    ActionProposalSpec(
                        action_type="recover_production_render",
                        label="Recover render",
                        description=(
                            "Create one idempotent render-recovery generation for "
                            "this unresolved dead-letter production."
                        ),
                        payload={"production_id": production_id},
                    )
                )
                break

    if intent == "performance_advice":
        specs.append(
            ActionProposalSpec(
                action_type="refresh_channel_intelligence",
                label="Refresh channel intelligence",
                description=(
                    "Recompute channel learning before making a new editing decision."
                ),
                payload={},
            )
        )

    if intent == "best_clips" and evidence:
        lead = evidence[0]
        payload: dict[str, object] = {"clip_id": str(lead["id"])}
        if blueprint_key:
            payload["edit_blueprint_key"] = blueprint_key
        specs.append(
            ActionProposalSpec(
                action_type="create_short_production",
                label="Make a short from top clip",
                description=(
                    "Start one channel-scoped production from the top grounded clip."
                ),
                payload=payload,
            )
        )

    if intent == "create_content" and request.selected_clip_ids:
        clip_ids = [str(value) for value in request.selected_clip_ids]
        if len(clip_ids) == 1:
            payload = {"clip_id": clip_ids[0]}
            if blueprint_key:
                payload["edit_blueprint_key"] = blueprint_key
            specs.append(
                ActionProposalSpec(
                    action_type="create_short_production",
                    label="Create production",
                    description=(
                        "Start one channel-scoped short from the selected clip."
                    ),
                    payload=payload,
                )
            )
        else:
            allowed = ranked_episode_allowed_counts(request.channel_profile_id)
            if len(clip_ids) in allowed:
                payload = {
                    "clip_ids": clip_ids,
                    "premise": request.prompt[:500],
                    "item_count": len(clip_ids),
                    "preserve_candidate_order": True,
                }
                if blueprint_key:
                    payload["edit_blueprint_key"] = blueprint_key
                specs.append(
                    ActionProposalSpec(
                        action_type="create_ranked_short_episode",
                        label=(
                            f"Create ranked episode from {len(clip_ids)} "
                            "selected clips"
                        ),
                        description=(
                            "Freeze exactly these clips in selection order, freeze "
                            "the channel brand/edit recipe, and start ranked-episode "
                            "editorial."
                        ),
                        payload=payload,
                    )
                )

    return specs[:4]


def _action_response(proposal: CommandActionProposal) -> CommandAction:
    return CommandAction(
        proposal_id=proposal.id,
        thread_id=proposal.thread_id,
        source_turn_id=proposal.source_turn_id,
        type=proposal.action_type,  # type: ignore[arg-type]
        label=proposal.label,
        description=proposal.description,
        status=proposal.status,
        expires_at=proposal.expires_at,
        payload=dict(proposal.payload or {}),
    )


def _proposal_status(
    proposal: CommandActionProposal,
) -> ActionProposalStatusResponse:
    return ActionProposalStatusResponse(
        proposal_id=proposal.id,
        request_id=proposal.request_id,
        thread_id=proposal.thread_id,
        source_turn_id=proposal.source_turn_id,
        channel_profile_id=proposal.channel_profile_id,
        action_type=proposal.action_type,
        label=proposal.label,
        description=proposal.description,
        status=proposal.status,
        execution_attempts=proposal.execution_attempts,
        expires_at=proposal.expires_at,
        confirmed_by=proposal.confirmed_by,
        confirmed_at=proposal.confirmed_at,
        execution_started_at=proposal.execution_started_at,
        executed_at=proposal.executed_at,
        result=dict(proposal.result or {}),
        error=proposal.error,
        payload=dict(proposal.payload or {}),
    )


@router.post("/command", response_model=CommandResponse)
def command(http_request: Request, request: CommandRequest) -> CommandResponse:
    require_control_scope(http_request, "ai:read")
    actor = control_actor(http_request)
    request_id = uuid.uuid4()

    thread: CommandThread | None = None
    prior_turns: list[CommandTurn] = []
    prior_proposals: list[CommandActionProposal] = []
    if request.thread_id is not None:
        try:
            thread = get_command_thread(
                request.thread_id,
                channel_profile_id=request.channel_profile_id,
            )
            if thread.status != "active":
                raise ValueError(
                    f"command thread is not active: {thread.status}"
                )
            prior_turns = list_command_turns(thread.id)
            prior_proposals = [
                get_action_proposal(row.id)
                for row in list_thread_proposals(thread.id)
            ]
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    latest_assistant = next(
        (turn for turn in reversed(prior_turns) if turn.role == "assistant"),
        None,
    )
    has_pending_proposal = bool(
        latest_assistant
        and any(
            proposal.source_turn_id == latest_assistant.id
            and proposal.status in {"proposed", "failed"}
            for proposal in prior_proposals
        )
    )
    resolution = resolve_command_follow_up(
        request.prompt,
        request.selected_clip_ids,
        prior_turns,
        has_pending_proposal=has_pending_proposal,
    )
    resolved_selected_clip_ids = list(resolution.selected_clip_ids)
    resolved_request = request.model_copy(
        update={"selected_clip_ids": resolved_selected_clip_ids}
    )
    intent = resolution.intent_hint or classify_intent(
        request.prompt,
        resolved_selected_clip_ids,
    )

    reused_proposals: list[CommandActionProposal] = []
    try:
        if intent == "confirm_action":
            reused_proposals = [
                proposal
                for proposal in prior_proposals
                if proposal.source_turn_id == resolution.action_source_turn_id
                and proposal.status in {"proposed", "failed"}
            ]
            deterministic = (
                "I did not execute anything from that chat message. "
                "Katcha requires the exact server-issued action payload to be "
                "reviewed and explicitly confirmed. I restored the pending "
                "proposal below so you can verify it before execution."
            )
            evidence = []
        elif intent == "best_clips":
            deterministic, evidence = best_clips(
                request.channel_profile_id,
                resolution.effective_prompt,
            )
        elif intent == "failures":
            deterministic, evidence = failures(request.channel_profile_id)
        elif intent in {"clip_rejection", "clip_explanation"}:
            clip_id = (
                resolved_selected_clip_ids[0]
                if resolved_selected_clip_ids
                else _uuid_from_prompt(request.prompt)
            )
            if clip_id is None:
                deterministic = (
                    "Select a clip or include its clip ID so I can explain the "
                    "stored scoring, analysis, and review evidence for that exact clip."
                )
                evidence = []
            else:
                deterministic, evidence = clip_explanation(
                    request.channel_profile_id,
                    clip_id,
                )
        elif intent == "performance_advice":
            deterministic, evidence = performance_advice(
                request.channel_profile_id,
                resolution.effective_prompt,
            )
        elif intent == "create_content":
            blueprint_key = infer_edit_blueprint_key(request.prompt)
            if not resolved_selected_clip_ids:
                deterministic = (
                    "Select the clip or clips you want to use first. I will not "
                    "silently substitute Katcha-selected media for a request that "
                    "refers to specific clips."
                )
            elif len(resolved_selected_clip_ids) == 1:
                deterministic = (
                    "I prepared a production proposal for the resolved clip. "
                    "Nothing will start until you confirm the server-issued proposal."
                )
            else:
                allowed = ranked_episode_allowed_counts(request.channel_profile_id)
                selected_count = len(resolved_selected_clip_ids)
                if not allowed:
                    deterministic = (
                        "This channel does not currently have an enabled ranked "
                        "episode format, so I cannot propose a multi-clip episode."
                    )
                elif selected_count not in allowed:
                    choices = ", ".join(str(value) for value in allowed)
                    deterministic = (
                        f"The resolved context contains {selected_count} clips, but "
                        f"this channel's ranked format supports {choices}. Adjust "
                        "the selection and I can prepare an exact locked-clip proposal."
                    )
                else:
                    recipe = blueprint_key or "the channel default edit recipe"
                    deterministic = (
                        f"I prepared a ranked-episode proposal using exactly "
                        f"{selected_count} resolved clips in order with {recipe}. "
                        "The channel brand and blueprint version will be frozen "
                        "when the action is executed."
                    )
            evidence = [
                {
                    "kind": "selection",
                    "id": "current",
                    "selected_clip_ids": [
                        str(value) for value in resolved_selected_clip_ids
                    ],
                    "selected_production_id": (
                        str(request.selected_production_id)
                        if request.selected_production_id
                        else None
                    ),
                    "requested_edit_blueprint_key": blueprint_key,
                    "conversation_source_turn_id": (
                        str(resolution.source_turn_id)
                        if resolution.source_turn_id
                        else None
                    ),
                }
            ]
        else:
            deterministic, evidence = channel_status(request.channel_profile_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    if resolution.inherited_from_thread and resolution.resolution:
        deterministic = f"{deterministic} Context: {resolution.resolution}"

    narrative = compose_grounded_answer(
        channel_profile_id=request.channel_profile_id,
        request_id=request_id,
        user_prompt=request.prompt,
        intent=intent,
        deterministic_answer=deterministic,
        evidence=evidence,
    )
    try:
        if thread is None:
            thread = create_command_thread(
                channel_profile_id=request.channel_profile_id,
                actor=actor,
                title=request.prompt,
                metadata={"surface": "katcha_ai_command_center"},
            )

        assistant_context: dict[str, object] = {
            "key_points": list(narrative.value.key_points),
            "caveats": list(narrative.value.caveats),
        }
        if resolution.action_source_turn_id is not None:
            assistant_context["action_source_turn_id"] = str(
                resolution.action_source_turn_id
            )

        user_turn, assistant_turn = record_command_exchange(
            thread_id=thread.id,
            request_id=request_id,
            user_content=request.prompt,
            assistant_content=narrative.value.answer,
            intent=intent,
            narrator=f"{narrative.target.provider}/{narrative.target.model}",
            evidence=evidence,
            user_context={
                "selected_clip_ids": [
                    str(value) for value in request.selected_clip_ids
                ],
                "resolved_selected_clip_ids": [
                    str(value) for value in resolved_selected_clip_ids
                ],
                "selected_production_id": (
                    str(request.selected_production_id)
                    if request.selected_production_id
                    else None
                ),
                "inherited_from_thread": resolution.inherited_from_thread,
                "context_source_turn_id": (
                    str(resolution.source_turn_id)
                    if resolution.source_turn_id
                    else None
                ),
                "context_resolution": resolution.resolution,
            },
            assistant_context=assistant_context,
        )

        if intent == "confirm_action":
            proposals = reused_proposals
        else:
            specs = _action_specs(resolved_request, intent, evidence)
            proposals = create_action_proposals(
                request_id=request_id,
                channel_profile_id=request.channel_profile_id,
                specs=specs,
                thread_id=thread.id,
                source_turn_id=assistant_turn.id,
            )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    return CommandResponse(
        request_id=request_id,
        thread_id=thread.id,
        user_turn_id=user_turn.id,
        assistant_turn_id=assistant_turn.id,
        channel_profile_id=request.channel_profile_id,
        intent=intent,
        answer=narrative.value.answer,
        key_points=narrative.value.key_points,
        caveats=narrative.value.caveats,
        evidence=evidence,
        actions=[_action_response(item) for item in proposals],
        resolved_context=ResolvedContextResponse(
            selected_clip_ids=resolved_selected_clip_ids,
            inherited_from_thread=resolution.inherited_from_thread,
            source_turn_id=resolution.source_turn_id,
            resolution=resolution.resolution,
            action_source_turn_id=resolution.action_source_turn_id,
        ),
        narrator=f"{narrative.target.provider}/{narrative.target.model}",
    )

@router.get("/threads", response_model=list[ThreadSummaryResponse])
def threads(
    http_request: Request,
    channel_profile_id: uuid.UUID,
    limit: int = 30,
) -> list[ThreadSummaryResponse]:
    require_control_scope(http_request, "ai:read")
    try:
        rows = list_command_threads(channel_profile_id, limit=limit)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return [_thread_summary(row) for row in rows]


@router.get(
    "/threads/{thread_id}",
    response_model=ThreadDetailResponse,
)
def thread_detail(
    thread_id: uuid.UUID,
    http_request: Request,
) -> ThreadDetailResponse:
    require_control_scope(http_request, "ai:read")
    try:
        thread = get_command_thread(thread_id)
        turns = list_command_turns(thread_id)
        proposals = [
            get_action_proposal(row.id)
            for row in list_thread_proposals(thread_id)
        ]
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return ThreadDetailResponse(
        thread=_thread_summary(thread),
        turns=[_turn_response(row) for row in turns],
        actions=[_proposal_status(row) for row in proposals],
    )


@router.post(
    "/threads/{thread_id}/archive",
    response_model=ThreadSummaryResponse,
)
def archive_thread(
    thread_id: uuid.UUID,
    http_request: Request,
) -> ThreadSummaryResponse:
    require_control_scope(http_request, "ai:read")
    actor = control_actor(http_request)
    try:
        thread = archive_command_thread(thread_id, actor=actor)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return _thread_summary(thread)


@router.get(
    "/actions/{proposal_id}/activity",
    response_model=ActionActivityResponse,
)
def action_activity(
    proposal_id: uuid.UUID,
    http_request: Request,
    limit: int = Query(default=20, ge=1, le=100),
) -> ActionActivityResponse:
    require_control_scope(http_request, "ai:read")
    try:
        get_action_proposal(proposal_id)
        activity = get_action_activity(proposal_id, limit=limit)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    resource = activity.resource
    return ActionActivityResponse(
        proposal_id=activity.proposal.id,
        action_type=activity.proposal.action_type,
        proposal_status=activity.proposal.status,
        workflow_id=activity.workflow_id,
        state=activity.state,
        settled=activity.settled,
        resource=(
            ActionResourceActivityResponse(
                kind=resource.kind,
                id=resource.id,
                workflow_id=resource.workflow_id,
                status=resource.status,
                stage=resource.stage,
                generation=resource.generation,
                error=resource.error,
                updated_at=resource.updated_at,
            )
            if resource is not None
            else None
        ),
        events=[
            ActionActivityEventResponse.model_validate(event)
            for event in activity.events
        ],
    )


@router.get(
    "/actions/{proposal_id}",
    response_model=ActionProposalStatusResponse,
)
def action_status(
    proposal_id: uuid.UUID,
    http_request: Request,
) -> ActionProposalStatusResponse:
    require_control_scope(http_request, "ai:read")
    try:
        proposal = get_action_proposal(proposal_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return _proposal_status(proposal)


async def _execute_proposal(
    proposal: CommandActionProposal,
    *,
    actor: str,
) -> dict[str, object]:
    payload = dict(proposal.payload or {})

    if proposal.action_type == "refresh_channel_intelligence":
        run_key = f"command-proposal-{proposal.id}"
        workflow_id = (
            f"channel-intelligence-refresh-{proposal.channel_profile_id}-"
            f"{proposal.id.hex[:20]}"
        )
        run = start_channel_intelligence_run(
            proposal.channel_profile_id,
            run_key=run_key,
            workflow_id=workflow_id,
        )
        try:
            await start_channel_intelligence_refresh(
                str(proposal.channel_profile_id),
                workflow_id,
                run_key,
            )
        except Exception as exc:
            fail_channel_intelligence_run(
                proposal.channel_profile_id,
                run_key=run_key,
                workflow_id=workflow_id,
                error=f"{type(exc).__name__}: {exc}",
            )
            raise
        return {
            "intelligence_run_id": str(run.id),
            "workflow_id": workflow_id,
            "run_key": run_key,
        }

    if proposal.action_type == "create_short_production":
        clip_id = uuid.UUID(str(payload["clip_id"]))
        production = register_short_production(
            clip_id,
            idempotency_key=proposal.idempotency_key,
            channel_profile_id=proposal.channel_profile_id,
            edit_blueprint_key=(
                str(payload["edit_blueprint_key"])
                if payload.get("edit_blueprint_key")
                else None
            ),
        )
        if production.status == ProductionStatus.QUEUED.value:
            await start_production_workflow(
                str(production.id),
                production.workflow_id,
            )
        return {
            "production_id": str(production.id),
            "workflow_id": production.workflow_id,
            "status": production.status,
            "edit_blueprint_key": production.edit_blueprint_key,
            "edit_blueprint_version": production.edit_blueprint_version,
        }

    if proposal.action_type == "create_ranked_short_episode":
        clip_ids = [uuid.UUID(str(value)) for value in payload["clip_ids"]]
        candidates = build_short_episode_candidates(clip_ids)
        episode = register_short_episode(
            channel_profile_id=proposal.channel_profile_id,
            premise=str(payload.get("premise") or "Katcha AI ranked episode"),
            candidates=candidates,
            item_count=int(payload.get("item_count") or len(candidates)),
            edit_blueprint_key=(
                str(payload["edit_blueprint_key"])
                if payload.get("edit_blueprint_key")
                else None
            ),
            idempotency_key=proposal.idempotency_key,
            planning_metadata={
                "command_proposal_id": str(proposal.id),
                "command_request_id": str(proposal.request_id),
                "confirmed_by": actor,
                "manual_clip_selection": True,
            },
            preserve_candidate_order=bool(
                payload.get("preserve_candidate_order", True)
            ),
        )
        workflow_id = f"{episode.workflow_id}-editorial-script"
        if episode.status == "planned":
            await start_short_episode_editorial_workflow(
                str(episode.id),
                workflow_id,
                start_stage="script",
            )
        return {
            "short_episode_id": str(episode.id),
            "workflow_id": workflow_id,
            "status": episode.status,
            "ordered_clip_ids": [str(value) for value in clip_ids],
            "edit_blueprint_key": episode.edit_blueprint_key,
            "edit_blueprint_version": episode.edit_blueprint_version,
            "brand_key": episode.brand_key,
            "brand_version": episode.brand_version,
        }

    if proposal.action_type == "recover_production_render":
        production_id = uuid.UUID(str(payload["production_id"]))
        with session_scope() as session:
            production = session.get(Production, production_id)
            if production is None:
                raise ValueError(f"production not found: {production_id}")
            if production.channel_profile_id != proposal.channel_profile_id:
                raise ValueError("production is outside the proposal channel")
        attempts = render_attempts_for_source("production", production_id)
        if not attempts or attempts[-1].status != "dead_letter":
            raise ValueError(
                "production does not have a current dead-letter render attempt"
            )
        child = register_regeneration(
            production_id,
            stage="render",
            note="Katcha AI operator-confirmed render recovery",
            actor=actor,
            idempotency_key=proposal.idempotency_key,
        )
        await start_production_workflow(
            str(child.id),
            child.workflow_id,
            start_stage="render",
        )
        return {
            "source_production_id": str(production_id),
            "child_production_id": str(child.id),
            "workflow_id": child.workflow_id,
        }

    raise ValueError(f"unsupported command action: {proposal.action_type}")


@router.post(
    "/actions/{proposal_id}/execute",
    response_model=ExecuteActionResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def execute_action(
    proposal_id: uuid.UUID,
    http_request: Request,
    request: ExecuteActionRequest,
) -> ExecuteActionResponse:
    if not request.confirmed:
        raise HTTPException(
            status_code=409,
            detail="explicit confirmation is required",
        )
    actor = control_actor(http_request)

    try:
        current = get_action_proposal(proposal_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    required_scope = _ACTION_SCOPES.get(current.action_type)
    if required_scope is None:
        raise HTTPException(status_code=409, detail="unsupported proposal action")
    require_control_scope(http_request, required_scope)

    try:
        claim = claim_action_proposal(proposal_id, actor=actor)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    if not claim.should_execute:
        proposal = claim.proposal
        return ExecuteActionResponse(
            proposal_id=proposal.id,
            action_type=proposal.action_type,
            status=proposal.status,
            execution_attempts=proposal.execution_attempts,
            result=dict(proposal.result or {}),
            error=proposal.error,
        )

    try:
        result = await _execute_proposal(claim.proposal, actor=actor)
        proposal = complete_action_proposal(proposal_id, result=result)
        return ExecuteActionResponse(
            proposal_id=proposal.id,
            action_type=proposal.action_type,
            status=proposal.status,
            execution_attempts=proposal.execution_attempts,
            result=dict(proposal.result or {}),
        )
    except (KeyError, TypeError, ValueError) as exc:
        proposal = fail_action_proposal(proposal_id, error=str(exc))
        raise HTTPException(status_code=409, detail=proposal.error) from exc
    except Exception as exc:
        proposal = fail_action_proposal(
            proposal_id,
            error=f"{type(exc).__name__}: {exc}",
        )
        raise HTTPException(
            status_code=503,
            detail=proposal.error,
        ) from exc
