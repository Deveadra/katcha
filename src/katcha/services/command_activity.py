from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select

from katcha.command_center_models import CommandActionProposal
from katcha.db import session_scope
from katcha.intelligence_models import ChannelIntelligenceRun
from katcha.models import DomainEvent
from katcha.production_models import Production
from katcha.short_episode_models import ShortEpisode

_SETTLED_STATUSES = {
    "review",
    "approved",
    "rejected",
    "failed",
    "completed",
    "published",
    "private",
    "unlisted",
    "scheduled",
}


@dataclass(frozen=True, slots=True)
class ActionResourceActivity:
    kind: str
    id: uuid.UUID
    workflow_id: str
    status: str
    stage: str
    generation: int
    error: str | None
    updated_at: datetime | None


@dataclass(frozen=True, slots=True)
class ActionActivity:
    proposal: CommandActionProposal
    workflow_id: str | None
    resource: ActionResourceActivity | None
    state: str
    settled: bool
    events: tuple[dict[str, object], ...]


def _uuid_result(
    result: dict[str, object],
    *keys: str,
) -> tuple[str, uuid.UUID] | None:
    for key in keys:
        raw = result.get(key)
        if not raw:
            continue
        try:
            value = uuid.UUID(str(raw))
        except (TypeError, ValueError):
            continue
        kind = "short_episode" if "episode" in key else "production"
        return kind, value
    return None


def _resource_activity(
    session: object,
    proposal: CommandActionProposal,
) -> ActionResourceActivity | None:
    result = dict(proposal.result or {})
    if (
        proposal.action_type == "refresh_channel_intelligence"
        and result.get("run_key")
    ):
        run = session.scalar(
            select(ChannelIntelligenceRun).where(
                ChannelIntelligenceRun.channel_profile_id
                == proposal.channel_profile_id,
                ChannelIntelligenceRun.run_key == str(result["run_key"]),
            )
        )
        if run is not None:
            if run.workflow_id != str(result.get("workflow_id") or ""):
                raise ValueError(
                    "intelligence run workflow does not match command action"
                )
            return ActionResourceActivity(
                kind="channel_intelligence_run",
                id=run.id,
                workflow_id=run.workflow_id,
                status=run.status,
                stage=run.stage,
                generation=1,
                error=run.error,
                updated_at=run.updated_at,
            )

    reference = _uuid_result(
        result,
        "child_production_id",
        "production_id",
        "short_episode_id",
    )
    if reference is None:
        return None
    kind, resource_id = reference
    if kind == "production":
        row = session.get(Production, resource_id)
    else:
        row = session.get(ShortEpisode, resource_id)
    if row is None:
        return None
    if row.channel_profile_id != proposal.channel_profile_id:
        raise ValueError("command action resource belongs to a different channel")
    return ActionResourceActivity(
        kind=kind,
        id=row.id,
        workflow_id=row.workflow_id,
        status=row.status,
        stage=row.stage,
        generation=int(getattr(row, "generation", 1) or 1),
        error=getattr(row, "error", None),
        updated_at=row.updated_at,
    )


def _event_rows(
    session: object,
    proposal: CommandActionProposal,
    resource: ActionResourceActivity | None,
    *,
    limit: int,
) -> tuple[dict[str, object], ...]:
    clauses = [
        (
            DomainEvent.aggregate_type == "command_action_proposal",
            DomainEvent.aggregate_id == str(proposal.id),
        )
    ]
    if resource is not None:
        clauses.append(
            (
                DomainEvent.aggregate_type == resource.kind,
                DomainEvent.aggregate_id == str(resource.id),
            )
        )

    rows: list[DomainEvent] = []
    for aggregate_type_clause, aggregate_id_clause in clauses:
        rows.extend(
            session.scalars(
                select(DomainEvent)
                .where(aggregate_type_clause, aggregate_id_clause)
                .order_by(DomainEvent.created_at.desc(), DomainEvent.id.desc())
                .limit(limit)
            )
        )
    rows.sort(key=lambda row: (row.created_at, str(row.id)))
    rows = rows[-limit:]
    return tuple(
        {
            "id": str(row.id),
            "event_type": row.event_type,
            "aggregate_type": row.aggregate_type,
            "aggregate_id": row.aggregate_id,
            "created_at": row.created_at,
        }
        for row in rows
    )


def _state(
    proposal: CommandActionProposal,
    resource: ActionResourceActivity | None,
) -> tuple[str, bool]:
    if proposal.status in {"expired", "failed"}:
        return proposal.status, True
    if proposal.status in {"proposed", "executing"}:
        return proposal.status, False
    if resource is None:
        if proposal.status == "executed":
            return "workflow_started", False
        return proposal.status, False

    status = resource.status.casefold()
    if status == "failed":
        return "failed", True
    if status == "review":
        return "awaiting_review", True
    if status in {"approved", "rejected"}:
        return status, True
    return status, status in _SETTLED_STATUSES


def get_action_activity(
    proposal_id: uuid.UUID,
    *,
    limit: int = 20,
) -> ActionActivity:
    if limit < 1 or limit > 100:
        raise ValueError("activity event limit must be between 1 and 100")

    with session_scope() as session:
        proposal = session.get(CommandActionProposal, proposal_id)
        if proposal is None:
            raise ValueError(f"command action proposal not found: {proposal_id}")
        resource = _resource_activity(session, proposal)
        state, settled = _state(proposal, resource)
        result = dict(proposal.result or {})
        workflow_id = (
            str(result["workflow_id"]) if result.get("workflow_id") else None
        )
        events = _event_rows(
            session,
            proposal,
            resource,
            limit=limit,
        )
        session.expunge(proposal)
        return ActionActivity(
            proposal=proposal,
            workflow_id=workflow_id,
            resource=resource,
            state=state,
            settled=settled,
            events=events,
        )
