from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import desc, func, select

from katcha.command_center_models import (
    CommandActionProposal,
    CommandThread,
    CommandTurn,
)
from katcha.db import session_scope
from katcha.models import DomainEvent
from katcha.services.channel_profiles import ensure_active_profile


def _now() -> datetime:
    return datetime.now(UTC)


def _title(value: str) -> str:
    compact = " ".join(value.strip().split())
    if not compact:
        return "New Katcha AI conversation"
    return compact[:157] + "..." if len(compact) > 160 else compact


def create_command_thread(
    *,
    channel_profile_id: uuid.UUID,
    actor: str,
    title: str,
    metadata: dict[str, object] | None = None,
) -> CommandThread:
    now = _now()
    with session_scope() as session:
        ensure_active_profile(session, channel_profile_id)
        thread = CommandThread(
            channel_profile_id=channel_profile_id,
            title=_title(title),
            status="active",
            created_by=actor,
            thread_metadata=dict(metadata or {}),
            last_activity_at=now,
        )
        session.add(thread)
        session.flush()
        session.add(
            DomainEvent(
                aggregate_type="command_thread",
                aggregate_id=str(thread.id),
                event_type="command_center.thread_created",
                payload={
                    "thread_id": str(thread.id),
                    "channel_profile_id": str(channel_profile_id),
                    "actor": actor,
                    "title": thread.title,
                },
            )
        )
        session.refresh(thread)
        session.expunge(thread)
        return thread


def get_command_thread(
    thread_id: uuid.UUID,
    *,
    channel_profile_id: uuid.UUID | None = None,
) -> CommandThread:
    with session_scope() as session:
        thread = session.get(CommandThread, thread_id)
        if thread is None:
            raise ValueError(f"command thread not found: {thread_id}")
        if (
            channel_profile_id is not None
            and thread.channel_profile_id != channel_profile_id
        ):
            raise ValueError("command thread belongs to a different channel")
        session.expunge(thread)
        return thread


def list_command_threads(
    channel_profile_id: uuid.UUID,
    *,
    limit: int = 30,
    include_archived: bool = False,
) -> list[CommandThread]:
    if limit < 1 or limit > 100:
        raise ValueError("thread limit must be between 1 and 100")
    with session_scope() as session:
        ensure_active_profile(session, channel_profile_id)
        stmt = select(CommandThread).where(
            CommandThread.channel_profile_id == channel_profile_id
        )
        if not include_archived:
            stmt = stmt.where(CommandThread.status == "active")
        rows = list(
            session.scalars(
                stmt.order_by(
                    desc(CommandThread.last_activity_at),
                    desc(CommandThread.created_at),
                ).limit(limit)
            )
        )
        for row in rows:
            session.expunge(row)
        return rows


def append_command_turn(
    *,
    thread_id: uuid.UUID,
    role: str,
    content: str,
    request_id: uuid.UUID | None = None,
    intent: str | None = None,
    narrator: str | None = None,
    evidence: list[dict[str, object]] | None = None,
    context: dict[str, object] | None = None,
) -> CommandTurn:
    normalized_role = role.strip().casefold()
    if normalized_role not in {"user", "assistant", "system"}:
        raise ValueError("command turn role must be user, assistant, or system")
    normalized_content = content.strip()
    if not normalized_content:
        raise ValueError("command turn content cannot be empty")

    now = _now()
    with session_scope() as session:
        thread = session.scalar(
            select(CommandThread)
            .where(CommandThread.id == thread_id)
            .with_for_update()
        )
        if thread is None:
            raise ValueError(f"command thread not found: {thread_id}")
        if thread.status != "active":
            raise ValueError(
                f"command thread is not active: {thread.status}"
            )
        sequence = int(
            session.scalar(
                select(
                    func.coalesce(func.max(CommandTurn.sequence_number), 0)
                ).where(CommandTurn.thread_id == thread_id)
            )
            or 0
        ) + 1
        turn = CommandTurn(
            thread_id=thread.id,
            channel_profile_id=thread.channel_profile_id,
            sequence_number=sequence,
            role=normalized_role,
            request_id=request_id,
            intent=intent,
            narrator=narrator,
            content=normalized_content,
            evidence=list(evidence or []),
            turn_context=dict(context or {}),
        )
        thread.last_activity_at = now
        session.add(turn)
        session.flush()
        session.add(
            DomainEvent(
                aggregate_type="command_thread",
                aggregate_id=str(thread.id),
                event_type="command_center.turn_recorded",
                payload={
                    "thread_id": str(thread.id),
                    "turn_id": str(turn.id),
                    "channel_profile_id": str(thread.channel_profile_id),
                    "request_id": str(request_id) if request_id else None,
                    "sequence_number": sequence,
                    "role": normalized_role,
                    "intent": intent,
                },
            )
        )
        session.refresh(turn)
        session.expunge(turn)
        return turn


def record_command_exchange(
    *,
    thread_id: uuid.UUID,
    request_id: uuid.UUID,
    user_content: str,
    assistant_content: str,
    intent: str,
    narrator: str,
    evidence: list[dict[str, object]] | None = None,
    user_context: dict[str, object] | None = None,
    assistant_context: dict[str, object] | None = None,
) -> tuple[CommandTurn, CommandTurn]:
    user_text = user_content.strip()
    assistant_text = assistant_content.strip()
    if not user_text or not assistant_text:
        raise ValueError("command exchange content cannot be empty")

    now = _now()
    with session_scope() as session:
        thread = session.scalar(
            select(CommandThread)
            .where(CommandThread.id == thread_id)
            .with_for_update()
        )
        if thread is None:
            raise ValueError(f"command thread not found: {thread_id}")
        if thread.status != "active":
            raise ValueError(
                f"command thread is not active: {thread.status}"
            )
        sequence = int(
            session.scalar(
                select(
                    func.coalesce(func.max(CommandTurn.sequence_number), 0)
                ).where(CommandTurn.thread_id == thread_id)
            )
            or 0
        )
        user_turn = CommandTurn(
            thread_id=thread.id,
            channel_profile_id=thread.channel_profile_id,
            sequence_number=sequence + 1,
            role="user",
            request_id=request_id,
            intent=None,
            narrator=None,
            content=user_text,
            evidence=[],
            turn_context=dict(user_context or {}),
        )
        assistant_turn = CommandTurn(
            thread_id=thread.id,
            channel_profile_id=thread.channel_profile_id,
            sequence_number=sequence + 2,
            role="assistant",
            request_id=request_id,
            intent=intent,
            narrator=narrator,
            content=assistant_text,
            evidence=list(evidence or []),
            turn_context=dict(assistant_context or {}),
        )
        thread.last_activity_at = now
        session.add_all([user_turn, assistant_turn])
        session.flush()
        for turn in (user_turn, assistant_turn):
            session.add(
                DomainEvent(
                    aggregate_type="command_thread",
                    aggregate_id=str(thread.id),
                    event_type="command_center.turn_recorded",
                    payload={
                        "thread_id": str(thread.id),
                        "turn_id": str(turn.id),
                        "channel_profile_id": str(thread.channel_profile_id),
                        "request_id": str(request_id),
                        "sequence_number": turn.sequence_number,
                        "role": turn.role,
                        "intent": turn.intent,
                    },
                )
            )
        session.flush()
        session.refresh(user_turn)
        session.refresh(assistant_turn)
        session.expunge(user_turn)
        session.expunge(assistant_turn)
        return user_turn, assistant_turn


def finalize_command_answer(
    assistant_turn_id: uuid.UUID,
    *,
    content: str,
    narrator: str,
    evidence: list[dict[str, object]],
    context: dict[str, object],
) -> None:
    """Persist the observed outcome after actions; never leave a pre-execution claim."""
    with session_scope() as session:
        turn = session.get(CommandTurn, assistant_turn_id)
        if turn is None or turn.role != "assistant":
            raise ValueError("The assistant command turn could not be finalized")
        turn.content = content
        turn.narrator = narrator
        turn.evidence = list(evidence)
        turn.turn_context = dict(context)
        session.add(DomainEvent(
            aggregate_type="command_thread", aggregate_id=str(turn.thread_id),
            event_type="command_center.answer_finalized",
            payload={"turn_id": str(turn.id), "request_id": str(turn.request_id)},
        ))


def list_command_turns(thread_id: uuid.UUID) -> list[CommandTurn]:
    with session_scope() as session:
        thread = session.get(CommandThread, thread_id)
        if thread is None:
            raise ValueError(f"command thread not found: {thread_id}")
        rows = list(
            session.scalars(
                select(CommandTurn)
                .where(CommandTurn.thread_id == thread_id)
                .order_by(CommandTurn.sequence_number)
            )
        )
        for row in rows:
            session.expunge(row)
        return rows


def list_thread_proposals(
    thread_id: uuid.UUID,
) -> list[CommandActionProposal]:
    with session_scope() as session:
        thread = session.get(CommandThread, thread_id)
        if thread is None:
            raise ValueError(f"command thread not found: {thread_id}")
        rows = list(
            session.scalars(
                select(CommandActionProposal)
                .where(CommandActionProposal.thread_id == thread_id)
                .order_by(CommandActionProposal.created_at)
            )
        )
        for row in rows:
            session.expunge(row)
        return rows


def archive_command_thread(
    thread_id: uuid.UUID,
    *,
    actor: str,
) -> CommandThread:
    now = _now()
    with session_scope() as session:
        thread = session.scalar(
            select(CommandThread)
            .where(CommandThread.id == thread_id)
            .with_for_update()
        )
        if thread is None:
            raise ValueError(f"command thread not found: {thread_id}")
        if thread.status == "archived":
            session.expunge(thread)
            return thread
        thread.status = "archived"
        thread.archived_at = now
        thread.last_activity_at = now
        session.add(
            DomainEvent(
                aggregate_type="command_thread",
                aggregate_id=str(thread.id),
                event_type="command_center.thread_archived",
                payload={
                    "thread_id": str(thread.id),
                    "channel_profile_id": str(thread.channel_profile_id),
                    "actor": actor,
                },
            )
        )
        session.flush()
        session.refresh(thread)
        session.expunge(thread)
        return thread
