from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from katcha.command_center_models import CommandActionProposal
from katcha.db import session_scope
from katcha.models import DomainEvent
from katcha.services.channel_profiles import ensure_active_profile


@dataclass(frozen=True, slots=True)
class ActionProposalSpec:
    action_type: str
    label: str
    description: str
    payload: dict[str, object]


@dataclass(frozen=True, slots=True)
class ProposalClaim:
    proposal: CommandActionProposal
    should_execute: bool


def _now() -> datetime:
    return datetime.now(UTC)


def create_action_proposals(
    *,
    request_id: uuid.UUID,
    channel_profile_id: uuid.UUID,
    specs: list[ActionProposalSpec],
    ttl_minutes: int = 30,
) -> list[CommandActionProposal]:
    created: list[CommandActionProposal] = []
    expires_at = _now() + timedelta(minutes=ttl_minutes)
    with session_scope() as session:
        ensure_active_profile(session, channel_profile_id)
        for spec in specs:
            proposal_id = uuid.uuid4()
            proposal = CommandActionProposal(
                id=proposal_id,
                request_id=request_id,
                channel_profile_id=channel_profile_id,
                action_type=spec.action_type,
                label=spec.label,
                description=spec.description,
                payload=dict(spec.payload),
                status="proposed",
                idempotency_key=f"command-proposal:{proposal_id}",
                execution_attempts=0,
                expires_at=expires_at,
                result={},
            )
            session.add(proposal)
            session.add(
                DomainEvent(
                    aggregate_type="command_action_proposal",
                    aggregate_id=str(proposal.id),
                    event_type="command_center.proposal_created",
                    payload={
                        "proposal_id": str(proposal.id),
                        "request_id": str(request_id),
                        "channel_profile_id": str(channel_profile_id),
                        "action_type": spec.action_type,
                        "expires_at": expires_at.isoformat(),
                    },
                )
            )
            session.flush()
            session.refresh(proposal)
            session.expunge(proposal)
            created.append(proposal)
    return created


def get_action_proposal(proposal_id: uuid.UUID) -> CommandActionProposal:
    with session_scope() as session:
        proposal = session.get(CommandActionProposal, proposal_id)
        if proposal is None:
            raise ValueError(f"command action proposal not found: {proposal_id}")
        if proposal.status == "proposed" and proposal.expires_at <= _now():
            proposal.status = "expired"
            session.add(
                DomainEvent(
                    aggregate_type="command_action_proposal",
                    aggregate_id=str(proposal.id),
                    event_type="command_center.proposal_expired",
                    payload={
                        "proposal_id": str(proposal.id),
                        "channel_profile_id": str(proposal.channel_profile_id),
                    },
                )
            )
            session.flush()
        session.refresh(proposal)
        session.expunge(proposal)
        return proposal


def claim_action_proposal(
    proposal_id: uuid.UUID,
    *,
    actor: str,
) -> ProposalClaim:
    with session_scope() as session:
        proposal = session.scalar(
            select(CommandActionProposal)
            .where(CommandActionProposal.id == proposal_id)
            .with_for_update()
        )
        if proposal is None:
            raise ValueError(f"command action proposal not found: {proposal_id}")
        now = _now()
        if proposal.expires_at <= now and proposal.status not in {"executed"}:
            proposal.status = "expired"
            raise ValueError("command action proposal has expired")
        if proposal.status == "executed":
            session.expunge(proposal)
            return ProposalClaim(proposal=proposal, should_execute=False)
        if proposal.status == "executing":
            session.expunge(proposal)
            return ProposalClaim(proposal=proposal, should_execute=False)
        if proposal.status not in {"proposed", "failed"}:
            raise ValueError(
                f"command action proposal cannot execute from status {proposal.status}"
            )

        proposal.status = "executing"
        proposal.confirmed_by = actor
        proposal.confirmed_at = proposal.confirmed_at or now
        proposal.execution_started_at = now
        proposal.execution_attempts += 1
        proposal.error = None
        session.add(
            DomainEvent(
                aggregate_type="command_action_proposal",
                aggregate_id=str(proposal.id),
                event_type="command_center.action_confirmed",
                payload={
                    "proposal_id": str(proposal.id),
                    "request_id": str(proposal.request_id),
                    "channel_profile_id": str(proposal.channel_profile_id),
                    "action_type": proposal.action_type,
                    "actor": actor,
                    "execution_attempt": proposal.execution_attempts,
                },
            )
        )
        session.flush()
        session.refresh(proposal)
        session.expunge(proposal)
        return ProposalClaim(proposal=proposal, should_execute=True)


def complete_action_proposal(
    proposal_id: uuid.UUID,
    *,
    result: dict[str, object],
) -> CommandActionProposal:
    with session_scope() as session:
        proposal = session.scalar(
            select(CommandActionProposal)
            .where(CommandActionProposal.id == proposal_id)
            .with_for_update()
        )
        if proposal is None:
            raise ValueError(f"command action proposal not found: {proposal_id}")
        proposal.status = "executed"
        proposal.executed_at = _now()
        proposal.result = dict(result)
        proposal.error = None
        session.add(
            DomainEvent(
                aggregate_type="command_action_proposal",
                aggregate_id=str(proposal.id),
                event_type="command_center.action_executed",
                payload={
                    "proposal_id": str(proposal.id),
                    "request_id": str(proposal.request_id),
                    "channel_profile_id": str(proposal.channel_profile_id),
                    "action_type": proposal.action_type,
                    "actor": proposal.confirmed_by,
                    "result": dict(result),
                },
            )
        )
        session.flush()
        session.refresh(proposal)
        session.expunge(proposal)
        return proposal


def fail_action_proposal(
    proposal_id: uuid.UUID,
    *,
    error: str,
) -> CommandActionProposal:
    with session_scope() as session:
        proposal = session.scalar(
            select(CommandActionProposal)
            .where(CommandActionProposal.id == proposal_id)
            .with_for_update()
        )
        if proposal is None:
            raise ValueError(f"command action proposal not found: {proposal_id}")
        proposal.status = "failed"
        proposal.error = error[:4000]
        session.add(
            DomainEvent(
                aggregate_type="command_action_proposal",
                aggregate_id=str(proposal.id),
                event_type="command_center.action_failed",
                payload={
                    "proposal_id": str(proposal.id),
                    "request_id": str(proposal.request_id),
                    "channel_profile_id": str(proposal.channel_profile_id),
                    "action_type": proposal.action_type,
                    "actor": proposal.confirmed_by,
                    "error": proposal.error,
                },
            )
        )
        session.flush()
        session.refresh(proposal)
        session.expunge(proposal)
        return proposal
