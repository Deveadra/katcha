from __future__ import annotations

import uuid
from typing import Literal

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field

from katcha.ai.command_center import compose_grounded_answer
from katcha.db import session_scope
from katcha.domain import CompilationStatus, ProductionStatus
from katcha.models import DomainEvent
from katcha.orchestration.client import (
    start_channel_intelligence_refresh,
    start_longform_workflow,
    start_production_workflow,
)
from katcha.orchestration.trend_client import (
    start_topic_watch_schedule,
    start_topic_watch_workflow,
)
from katcha.services.channel_editorial import freeze_channel_compilation_candidates
from katcha.services.channel_profiles import ensure_active_profile
from katcha.services.command_center import (
    best_clips,
    channel_status,
    classify_intent,
    clip_explanation,
    failures,
    performance_advice,
    source_discovery_plan,
)
from katcha.services.compilations import register_compilation
from katcha.services.discovery_trends import create_topic_watch_version
from katcha.services.productions import (
    register_regeneration,
    register_short_production,
)
from katcha.services.render_recovery import render_attempts_for_source

router = APIRouter(prefix="/v1/ai", tags=["katcha-ai"])


class CommandRequest(BaseModel):
    channel_profile_id: uuid.UUID
    prompt: str = Field(min_length=1, max_length=4000)
    selected_clip_ids: list[uuid.UUID] = Field(default_factory=list, max_length=20)
    selected_production_id: uuid.UUID | None = None


class EvidenceRecord(BaseModel):
    kind: str
    id: str
    title: str | None = None
    payload: dict[str, object]


class CommandAction(BaseModel):
    id: str
    type: Literal[
        "refresh_channel_intelligence",
        "create_short_production",
        "create_compilation",
        "recover_production_render",
        "start_source_scout",
    ]
    label: str
    description: str
    requires_confirmation: bool = True
    payload: dict[str, object] = Field(default_factory=dict)


class CommandResponse(BaseModel):
    request_id: uuid.UUID
    channel_profile_id: uuid.UUID
    intent: str
    answer: str
    key_points: list[str] = Field(default_factory=list)
    caveats: list[str] = Field(default_factory=list)
    evidence: list[dict[str, object]]
    actions: list[CommandAction]
    grounded: bool = True
    narrator: str


class ExecuteActionRequest(BaseModel):
    channel_profile_id: uuid.UUID
    action_type: Literal[
        "refresh_channel_intelligence",
        "create_short_production",
        "create_compilation",
        "recover_production_render",
        "start_source_scout",
    ]
    payload: dict[str, object] = Field(default_factory=dict)
    confirmed: bool
    actor: str = Field(default="operator:katcha-ai", min_length=1, max_length=128)


class ExecuteActionResponse(BaseModel):
    action_type: str
    status: str
    result: dict[str, object]


def _uuid_from_prompt(prompt: str) -> uuid.UUID | None:
    for token in prompt.replace(",", " ").replace(".", " ").split():
        try:
            return uuid.UUID(token.strip("()[]{}"))
        except ValueError:
            continue
    return None


def _actions(
    request: CommandRequest,
    intent: str,
    evidence: list[dict[str, object]],
) -> list[CommandAction]:
    actions: list[CommandAction] = []
    if intent == "failures":
        for item in evidence:
            production_id = (
                item.get("production_id")
                if item.get("kind") == "render_attempt"
                else None
            )
            if item.get("kind") == "production":
                production_id = item.get("id")
            if production_id and item.get("status") in {"dead_letter", "failed", "retry_exhausted"}:
                actions.append(
                    CommandAction(
                        id=f"recover:{production_id}",
                        type="recover_production_render",
                        label="Recover render",
                        description=(
                            "Create a new render generation through Katcha's "
                            "existing recovery path."
                        ),
                        payload={"production_id": str(production_id)},
                    )
                )
                break
    if intent == "source_discovery" and evidence:
        overview = evidence[0]
        if bool(overview.get("web_scout_ready")):
            platforms = [
                str(value)
                for value in (overview.get("requested_platforms") or [])
                if str(value)
            ]
            terms = [
                str(value)
                for value in (overview.get("suggested_terms") or [])
                if str(value)
            ]
            actions.append(
                CommandAction(
                    id=f"source-scout:{request.channel_profile_id}:{uuid.uuid4().hex[:8]}",
                    type="start_source_scout",
                    label="Start autonomous source scout",
                    description=(
                        "Search the wider public web now and every hour, including new creators, "
                        "communities, and sites. Results enter Katcha discovery and trend scoring; "
                        "they are not automatically rendered or published."
                    ),
                    payload={
                        "operator_request": request.prompt[:1000],
                        "platforms": platforms,
                        "terms": terms,
                        "interval_minutes": 60,
                        "top_n": 50,
                        "continuous": True,
                    },
                )
            )
    if intent == "performance_advice":
        actions.append(
            CommandAction(
                id=f"refresh:{request.channel_profile_id}",
                type="refresh_channel_intelligence",
                label="Refresh channel intelligence",
                description="Recompute channel learning before making a new editing decision.",
            )
        )
    if intent == "best_clips" and evidence:
        lead = evidence[0]
        actions.append(
            CommandAction(
                id=f"produce:{lead['id']}",
                type="create_short_production",
                label="Make a short from top clip",
                description=(
                    "Start a channel-scoped production using the selected clip "
                    "and current channel defaults."
                ),
                payload={"clip_id": str(lead["id"])},
            )
        )
    if intent == "create_content":
        clip_ids = [str(value) for value in request.selected_clip_ids]
        if len(clip_ids) == 1:
            actions.append(
                CommandAction(
                    id=f"produce:{clip_ids[0]}",
                    type="create_short_production",
                    label="Create production",
                    description="Start one channel-scoped short production from the selected clip.",
                    payload={"clip_id": clip_ids[0]},
                )
            )
        else:
            count = len(clip_ids) if clip_ids else 5
            actions.append(
                CommandAction(
                    id=f"compilation:{uuid.uuid4()}",
                    type="create_compilation",
                    label="Create ranked episode",
                    description=(
                        "Start a channel-scoped compilation. Katcha will use its existing "
                        "candidate-freeze policy; exact manual clip locking is "
                        "not yet part of this action contract."
                    ),
                    payload={
                        "theme": request.prompt[:500],
                        "target_segment_count": max(3, min(100, count)),
                    },
                )
            )
    return actions[:4]


@router.post("/command", response_model=CommandResponse)
def command(request: CommandRequest) -> CommandResponse:
    request_id = uuid.uuid4()
    intent = classify_intent(request.prompt, request.selected_clip_ids)
    try:
        if intent == "best_clips":
            deterministic, evidence = best_clips(
                request.channel_profile_id,
                request.prompt,
            )
        elif intent == "failures":
            deterministic, evidence = failures(request.channel_profile_id)
        elif intent in {"clip_rejection", "clip_explanation"}:
            clip_id = (
                request.selected_clip_ids[0]
                if request.selected_clip_ids
                else _uuid_from_prompt(request.prompt)
            )
            if clip_id is None:
                deterministic = (
                    "Select a clip or include its clip ID so I can explain the stored scoring, "
                    "analysis, and review evidence for that exact clip."
                )
                evidence = []
            else:
                deterministic, evidence = clip_explanation(
                    request.channel_profile_id,
                    clip_id,
                )
        elif intent == "performance_advice":
            deterministic, evidence = performance_advice(request.channel_profile_id)
        elif intent == "source_discovery":
            deterministic, evidence = source_discovery_plan(
                request.channel_profile_id,
                request.prompt,
            )
        elif intent == "create_content":
            deterministic = (
                "I can prepare an executable Katcha action, but I will not start production from "
                "natural language alone. Review the proposed action and confirm it explicitly."
            )
            evidence = [
                {
                    "kind": "selection",
                    "id": "current",
                    "selected_clip_ids": [str(value) for value in request.selected_clip_ids],
                    "selected_production_id": (
                        str(request.selected_production_id)
                        if request.selected_production_id
                        else None
                    ),
                }
            ]
        else:
            deterministic, evidence = channel_status(request.channel_profile_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    narrative = compose_grounded_answer(
        channel_profile_id=request.channel_profile_id,
        request_id=request_id,
        user_prompt=request.prompt,
        intent=intent,
        deterministic_answer=deterministic,
        evidence=evidence,
    )
    return CommandResponse(
        request_id=request_id,
        channel_profile_id=request.channel_profile_id,
        intent=intent,
        answer=narrative.value.answer,
        key_points=narrative.value.key_points,
        caveats=narrative.value.caveats,
        evidence=evidence,
        actions=_actions(request, intent, evidence),
        narrator=f"{narrative.target.provider}/{narrative.target.model}",
    )


def _audit_action(
    channel_profile_id: uuid.UUID,
    *,
    action_type: str,
    actor: str,
    payload: dict[str, object],
    result: dict[str, object],
) -> None:
    with session_scope() as session:
        session.add(
            DomainEvent(
                aggregate_type="channel_profile",
                aggregate_id=str(channel_profile_id),
                event_type="command_center.action_executed",
                payload={
                    "channel_profile_id": str(channel_profile_id),
                    "action_type": action_type,
                    "actor": actor,
                    "request_payload": payload,
                    "result": result,
                },
            )
        )


@router.post(
    "/actions/execute",
    response_model=ExecuteActionResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def execute_action(request: ExecuteActionRequest) -> ExecuteActionResponse:
    if not request.confirmed:
        raise HTTPException(status_code=409, detail="explicit confirmation is required")

    try:
        with session_scope() as session:
            ensure_active_profile(session, request.channel_profile_id)

        if request.action_type == "refresh_channel_intelligence":
            run_key = f"katcha-ai-{uuid.uuid4().hex}"
            workflow_id = (
                f"channel-intelligence-refresh-{request.channel_profile_id}-"
                f"{uuid.uuid4().hex[:20]}"
            )
            await start_channel_intelligence_refresh(
                str(request.channel_profile_id),
                workflow_id,
                run_key,
            )
            result = {"workflow_id": workflow_id, "run_key": run_key}

        elif request.action_type == "start_source_scout":
            raw_platforms = request.payload.get("platforms") or []
            if not isinstance(raw_platforms, list):
                raise ValueError("source scout platforms must be a list")
            allowed_platforms = {
                "tiktok",
                "instagram",
                "x",
                "bluesky",
                "youtube",
                "reddit",
                "discord",
                "web",
            }
            platforms = []
            for raw in raw_platforms:
                value = str(raw or "").strip().casefold()
                if value and value in allowed_platforms and value not in platforms:
                    platforms.append(value)

            raw_terms = request.payload.get("terms") or []
            if not isinstance(raw_terms, list):
                raise ValueError("source scout terms must be a list")
            terms = []
            for raw in raw_terms:
                value = str(raw or "").strip().casefold()
                if value and value not in terms:
                    terms.append(value)
                if len(terms) >= 12:
                    break

            interval_minutes = max(
                15,
                min(int(request.payload.get("interval_minutes") or 60), 24 * 60),
            )
            top_n = max(5, min(int(request.payload.get("top_n") or 50), 250))
            operator_request = str(
                request.payload.get("operator_request")
                or "Find new relevant public sources for this channel."
            )[:1000]
            continuous = bool(request.payload.get("continuous", True))
            watch_suffix = "-".join(platforms) if platforms else "wide-web"
            watch = create_topic_watch_version(
                watch_key=f"source-scout-{watch_suffix}"[:128],
                name=(
                    "Autonomous source scout · "
                    + (", ".join(platforms) if platforms else "wide web")
                )[:255],
                include_terms=terms,
                adapter_configs=[
                    {
                        "adapter_key": "web_scout",
                        "adapter_version": "v1",
                        "query": {
                            "operator_request": operator_request,
                            "platforms": platforms,
                            "limit": min(top_n * 2, 100),
                        },
                        "source_quota_limit_per_day": 24,
                        "provider_quota_limits": {
                            "openai.web_search": 24,
                        },
                    }
                ],
                freshness_horizon_hours=72,
                max_candidates=min(top_n * 2, 100),
                enabled=True,
                metadata={
                    "kind": "autonomous_source_scout",
                    "origin": "katcha_ai_command_center",
                    "requested_platforms": platforms,
                    "operator_request": operator_request,
                },
                channel_profile_id=request.channel_profile_id,
            )
            if continuous:
                workflow_id = f"topic-watch-schedule-{watch.id}"
                await start_topic_watch_schedule(
                    str(watch.id),
                    workflow_id,
                    interval_minutes=interval_minutes,
                    top_n=top_n,
                )
                result = {
                    "topic_watch_id": str(watch.id),
                    "watch_key": watch.watch_key,
                    "watch_version": watch.version,
                    "workflow_id": workflow_id,
                    "continuous": True,
                    "interval_minutes": interval_minutes,
                    "platforms": platforms,
                    "terms": terms,
                }
            else:
                execution_key = f"katcha-ai-source-scout-{uuid.uuid4().hex[:32]}"
                workflow_id = f"topic-watch-{watch.id}-{uuid.uuid4().hex[:20]}"
                await start_topic_watch_workflow(
                    str(watch.id),
                    workflow_id,
                    execution_key=execution_key,
                    top_n=top_n,
                )
                result = {
                    "topic_watch_id": str(watch.id),
                    "watch_key": watch.watch_key,
                    "watch_version": watch.version,
                    "workflow_id": workflow_id,
                    "execution_key": execution_key,
                    "continuous": False,
                    "platforms": platforms,
                    "terms": terms,
                }

        elif request.action_type == "create_short_production":
            clip_id = uuid.UUID(str(request.payload["clip_id"]))
            production = register_short_production(
                clip_id,
                persona_key=str(request.payload.get("persona_key") or "youth_host"),
                idempotency_key=str(
                    request.payload.get("idempotency_key")
                    or f"katcha-ai-{uuid.uuid4().hex}"
                ),
                channel_profile_id=request.channel_profile_id,
                edit_blueprint_key=(
                    str(request.payload["edit_blueprint_key"])
                    if request.payload.get("edit_blueprint_key")
                    else None
                ),
            )
            if production.status == ProductionStatus.QUEUED.value:
                await start_production_workflow(str(production.id), production.workflow_id)
            result = {
                "production_id": str(production.id),
                "workflow_id": production.workflow_id,
                "status": production.status,
            }

        elif request.action_type == "create_compilation":
            compilation = register_compilation(
                theme=str(request.payload.get("theme") or "Katcha AI episode")[:500],
                target_segment_count=int(
                    request.payload.get("target_segment_count") or 5
                ),
                persona_key=str(request.payload.get("persona_key") or "youth_host"),
                idempotency_key=str(
                    request.payload.get("idempotency_key")
                    or f"katcha-ai-{uuid.uuid4().hex}"
                ),
                channel_profile_id=request.channel_profile_id,
            )
            if compilation.status == CompilationStatus.QUEUED.value:
                compilation = freeze_channel_compilation_candidates(compilation.id)
                await start_longform_workflow(
                    str(compilation.id),
                    compilation.workflow_id,
                    start_stage="select",
                )
            result = {
                "compilation_id": str(compilation.id),
                "workflow_id": compilation.workflow_id,
                "status": compilation.status,
                "candidate_count": len(
                    (compilation.candidate_snapshot or {}).get("candidates") or []
                ),
            }

        else:
            production_id = uuid.UUID(str(request.payload["production_id"]))
            attempts = render_attempts_for_source("production", production_id)
            if not attempts or attempts[-1].status != "dead_letter":
                raise ValueError("production does not have a dead-letter render attempt")
            child = register_regeneration(
                production_id,
                stage="render",
                note="Katcha AI operator-confirmed render recovery",
                actor=request.actor,
            )
            await start_production_workflow(
                str(child.id),
                child.workflow_id,
                start_stage="render",
            )
            result = {
                "source_production_id": str(production_id),
                "child_production_id": str(child.id),
                "workflow_id": child.workflow_id,
            }

        _audit_action(
            request.channel_profile_id,
            action_type=request.action_type,
            actor=request.actor,
            payload=request.payload,
            result=result,
        )
        return ExecuteActionResponse(
            action_type=request.action_type,
            status="accepted",
            result=result,
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
