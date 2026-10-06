from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from katcha.command_center_models import (
    CommandActionProposal,
    CommandThread,
    CommandTurn,
)
from katcha.db import session_scope
from katcha.goal_models import CommandGoal, CommandGoalStep
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


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def create_action_proposals(
    *,
    request_id: uuid.UUID,
    channel_profile_id: uuid.UUID,
    specs: list[ActionProposalSpec],
    thread_id: uuid.UUID | None = None,
    source_turn_id: uuid.UUID | None = None,
    actor: str | None = None,
    credential_id: str | None = None,
    credential_fingerprint: str | None = None,
    ttl_minutes: int = 30,
) -> list[CommandActionProposal]:
    created: list[CommandActionProposal] = []
    expires_at = _now() + timedelta(minutes=ttl_minutes)
    with session_scope() as session:
        ensure_active_profile(session, channel_profile_id)
        if (thread_id is None) != (source_turn_id is None):
            raise ValueError(
                "thread_id and source_turn_id must be supplied together"
            )
        if thread_id is not None and source_turn_id is not None:
            thread = session.get(CommandThread, thread_id)
            turn = session.get(CommandTurn, source_turn_id)
            if thread is None:
                raise ValueError(f"command thread not found: {thread_id}")
            if turn is None:
                raise ValueError(f"command turn not found: {source_turn_id}")
            if thread.channel_profile_id != channel_profile_id:
                raise ValueError("command thread belongs to a different channel")
            if (
                turn.thread_id != thread_id
                or turn.channel_profile_id != channel_profile_id
                or turn.role != "assistant"
                or turn.request_id != request_id
            ):
                raise ValueError(
                    "proposal source turn does not match the request thread"
                )
        for spec in specs:
            proposal_id = uuid.uuid4()
            proposal = CommandActionProposal(
                id=proposal_id,
                request_id=request_id,
                thread_id=thread_id,
                source_turn_id=source_turn_id,
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
                        "thread_id": str(thread_id) if thread_id else None,
                        "source_turn_id": (
                            str(source_turn_id) if source_turn_id else None
                        ),
                        "channel_profile_id": str(channel_profile_id),
                        "action_type": spec.action_type,
                        "actor": actor,
                        "credential_id": credential_id,
                        "credential_fingerprint": credential_fingerprint,
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
        if proposal.status in {"proposed", "failed"} and _utc(proposal.expires_at) <= _now():
            proposal.status = "expired"
            session.add(
                DomainEvent(
                    aggregate_type="command_action_proposal",
                    aggregate_id=str(proposal.id),
                    event_type="command_center.proposal_expired",
                    payload={
                        "proposal_id": str(proposal.id),
                        "request_id": str(proposal.request_id),
                        "thread_id": (
                            str(proposal.thread_id) if proposal.thread_id else None
                        ),
                        "source_turn_id": (
                            str(proposal.source_turn_id)
                            if proposal.source_turn_id
                            else None
                        ),
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
    credential_id: str | None = None,
    credential_fingerprint: str | None = None,
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
        if _utc(proposal.expires_at) <= now and proposal.status not in {"executed"}:
            proposal.status = "expired"
            raise ValueError("command action proposal has expired")
        if proposal.status == "executed":
            session.expunge(proposal)
            return ProposalClaim(proposal=proposal, should_execute=False)
        if proposal.status == "executing":
            stale_before = now - timedelta(minutes=5)
            if (
                proposal.execution_started_at is not None
                and _utc(proposal.execution_started_at) > stale_before
            ):
                session.expunge(proposal)
                return ProposalClaim(proposal=proposal, should_execute=False)
            session.add(
                DomainEvent(
                    aggregate_type="command_action_proposal",
                    aggregate_id=str(proposal.id),
                    event_type="command_center.action_retry_claimed",
                    payload={
                        "proposal_id": str(proposal.id),
                        "request_id": str(proposal.request_id),
                        "thread_id": (
                            str(proposal.thread_id) if proposal.thread_id else None
                        ),
                        "source_turn_id": (
                            str(proposal.source_turn_id)
                            if proposal.source_turn_id
                            else None
                        ),
                        "channel_profile_id": str(proposal.channel_profile_id),
                        "action_type": proposal.action_type,
                        "actor": actor,
                        "credential_id": credential_id,
                        "credential_fingerprint": credential_fingerprint,
                        "previous_execution_started_at": (
                            proposal.execution_started_at.isoformat()
                            if proposal.execution_started_at
                            else None
                        ),
                    },
                )
            )
        elif proposal.status not in {"proposed", "failed"}:
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
                    "thread_id": (
                        str(proposal.thread_id) if proposal.thread_id else None
                    ),
                    "source_turn_id": (
                        str(proposal.source_turn_id)
                        if proposal.source_turn_id
                        else None
                    ),
                    "channel_profile_id": str(proposal.channel_profile_id),
                    "action_type": proposal.action_type,
                    "actor": actor,
                    "credential_id": credential_id,
                    "credential_fingerprint": credential_fingerprint,
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
    credential_id: str | None = None,
    credential_fingerprint: str | None = None,
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
                    "thread_id": (
                        str(proposal.thread_id) if proposal.thread_id else None
                    ),
                    "source_turn_id": (
                        str(proposal.source_turn_id)
                        if proposal.source_turn_id
                        else None
                    ),
                    "channel_profile_id": str(proposal.channel_profile_id),
                    "action_type": proposal.action_type,
                    "actor": proposal.confirmed_by,
                    "credential_id": credential_id,
                    "credential_fingerprint": credential_fingerprint,
                    "result": dict(result),
                },
            )
        )
        workflow_id = result.get("workflow_id")
        if workflow_id:
            session.add(
                DomainEvent(
                    aggregate_type="command_action_proposal",
                    aggregate_id=str(proposal.id),
                    event_type="command_center.workflow_started",
                    payload={
                        "proposal_id": str(proposal.id),
                        "request_id": str(proposal.request_id),
                        "thread_id": (
                            str(proposal.thread_id)
                            if proposal.thread_id
                            else None
                        ),
                        "source_turn_id": (
                            str(proposal.source_turn_id)
                            if proposal.source_turn_id
                            else None
                        ),
                        "channel_profile_id": str(proposal.channel_profile_id),
                        "action_type": proposal.action_type,
                        "actor": proposal.confirmed_by,
                        "credential_id": credential_id,
                        "credential_fingerprint": credential_fingerprint,
                        "workflow_id": str(workflow_id),
                        "result": dict(result),
                    },
                )
            )
        session.flush()
        session.refresh(proposal)
        session.expunge(proposal)
        return proposal


def reject_action_proposal(
    proposal_id: uuid.UUID,
    *,
    actor: str,
    reason: str = "",
    credential_id: str | None = None,
    credential_fingerprint: str | None = None,
) -> CommandActionProposal:
    with session_scope() as session:
        proposal = session.scalar(
            select(CommandActionProposal)
            .where(CommandActionProposal.id == proposal_id)
            .with_for_update()
        )
        if proposal is None:
            raise ValueError(f"command action proposal not found: {proposal_id}")
        if proposal.status == "rejected":
            session.expunge(proposal)
            return proposal
        if proposal.status not in {"proposed", "failed"}:
            raise ValueError(
                f"command action proposal cannot be rejected from status {proposal.status}"
            )
        proposal.status = "rejected"
        proposal.result = {
            "rejected": True,
            "reason": reason[:1000],
        }
        proposal.error = None

        goal_step = session.scalar(
            select(CommandGoalStep).where(
                CommandGoalStep.proposal_id == proposal.id
            )
        )
        if goal_step is not None:
            goal = session.scalar(
                select(CommandGoal)
                .where(CommandGoal.id == goal_step.goal_id)
                .with_for_update()
            )
            if goal is None:
                raise ValueError("saved goal for this proposal was not found")
            if goal.actor != actor:
                raise ValueError("This saved goal belongs to a different actor")
            rejection_result = {
                "rejected": True,
                "reason": reason[:1000],
                "proposal_id": str(proposal.id),
            }
            goal_step.status = "observed"
            goal_step.result = rejection_result
            goal_step.error = None
            if goal.status not in {
                "completed",
                "blocked",
                "needs_input",
                "failed",
                "cancelled",
            }:
                goal.observations = [
                    *list(goal.observations or []),
                    {
                        "step": goal_step.number,
                        "tool": goal_step.decision.get("tool"),
                        "arguments": goal_step.decision.get("arguments", {}),
                        "result": rejection_result,
                        "error": None,
                    },
                ]
                goal.step_count = max(goal.step_count, goal_step.number + 1)
                goal.status = "cancelled"
                goal.summary = "Proposal rejected. No action was applied."
                goal.result = {
                    "answer": goal.summary,
                    "thread_id": str(goal.thread_id),
                    "request_id": str(goal.command_id),
                    "intent": "goal_cancelled",
                    "narrator": "Katcha goal runner",
                    "evidence": [],
                    "actions": [],
                }

        session.add(
            DomainEvent(
                aggregate_type="command_action_proposal",
                aggregate_id=str(proposal.id),
                event_type="command_center.proposal_rejected",
                payload={
                    "proposal_id": str(proposal.id),
                    "request_id": str(proposal.request_id),
                    "thread_id": (
                        str(proposal.thread_id) if proposal.thread_id else None
                    ),
                    "source_turn_id": (
                        str(proposal.source_turn_id)
                        if proposal.source_turn_id
                        else None
                    ),
                    "channel_profile_id": str(proposal.channel_profile_id),
                    "action_type": proposal.action_type,
                    "actor": actor,
                    "credential_id": credential_id,
                    "credential_fingerprint": credential_fingerprint,
                    "reason": reason[:1000],
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
    credential_id: str | None = None,
    credential_fingerprint: str | None = None,
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
                    "thread_id": (
                        str(proposal.thread_id) if proposal.thread_id else None
                    ),
                    "source_turn_id": (
                        str(proposal.source_turn_id)
                        if proposal.source_turn_id
                        else None
                    ),
                    "channel_profile_id": str(proposal.channel_profile_id),
                    "action_type": proposal.action_type,
                    "actor": proposal.confirmed_by,
                    "credential_id": credential_id,
                    "credential_fingerprint": credential_fingerprint,
                    "error": proposal.error,
                },
            )
        )
        session.flush()
        session.refresh(proposal)
        session.expunge(proposal)
        return proposal
