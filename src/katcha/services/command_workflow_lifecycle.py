from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select

from katcha.acquisition_models import DiscoveryRun, TopicWatchVersion
from katcha.command_center_models import CommandActionProposal
from katcha.db import session_scope
from katcha.models import DomainEvent

_COMMAND_RUN_PREFIX = "command-proposal-"
_TERMINAL_STATES = {"completed", "failed"}
_CYCLE_STATES = {"completed", "failed"}


def proposal_id_from_command_run_key(run_key: str) -> uuid.UUID | None:
    value = str(run_key or "").strip()
    if not value.startswith(_COMMAND_RUN_PREFIX):
        return None
    raw = value.removeprefix(_COMMAND_RUN_PREFIX)
    try:
        return uuid.UUID(raw)
    except ValueError:
        return None


def _started_event(
    session: object,
    proposal_id: uuid.UUID,
) -> DomainEvent | None:
    return session.scalar(
        select(DomainEvent)
        .where(
            DomainEvent.aggregate_type == "command_action_proposal",
            DomainEvent.aggregate_id == str(proposal_id),
            DomainEvent.event_type == "command_center.workflow_started",
        )
        .order_by(DomainEvent.created_at.desc(), DomainEvent.id.desc())
        .limit(1)
    )


def _correlation_payload(
    proposal: CommandActionProposal,
    *,
    workflow_id: str,
) -> dict[str, object]:
    return {
        "proposal_id": str(proposal.id),
        "request_id": str(proposal.request_id),
        "thread_id": str(proposal.thread_id) if proposal.thread_id else None,
        "source_turn_id": (
            str(proposal.source_turn_id) if proposal.source_turn_id else None
        ),
        "channel_profile_id": str(proposal.channel_profile_id),
        "action_type": proposal.action_type,
        "actor": proposal.confirmed_by,
        "workflow_id": workflow_id,
    }


def _existing_event(
    session: object,
    *,
    proposal_id: uuid.UUID,
    event_type: str,
    workflow_id: str,
    cycle_key: str | None,
) -> DomainEvent | None:
    rows = list(
        session.scalars(
            select(DomainEvent)
            .where(
                DomainEvent.aggregate_type == "command_action_proposal",
                DomainEvent.aggregate_id == str(proposal_id),
                DomainEvent.event_type == event_type,
            )
            .order_by(DomainEvent.created_at.desc(), DomainEvent.id.desc())
            .limit(200)
        )
    )
    for row in rows:
        payload = dict(row.payload or {})
        if str(payload.get("workflow_id") or "") != workflow_id:
            continue
        if cycle_key is None:
            return row
        if str(payload.get("cycle_key") or "") == cycle_key:
            return row
    return None


def record_command_workflow_lifecycle(
    proposal_id: uuid.UUID,
    *,
    workflow_id: str,
    state: str,
    detail: dict[str, Any] | None = None,
    cycle_key: str | None = None,
) -> bool:
    normalized_state = state.strip().casefold()
    is_cycle = cycle_key is not None
    allowed = _CYCLE_STATES if is_cycle else _TERMINAL_STATES
    if normalized_state not in allowed:
        raise ValueError(
            f"unsupported command workflow lifecycle state: {normalized_state}"
        )

    event_type = (
        f"command_center.workflow_cycle_{normalized_state}"
        if is_cycle
        else f"command_center.workflow_{normalized_state}"
    )
    normalized_workflow_id = workflow_id.strip()
    if not normalized_workflow_id:
        raise ValueError("workflow_id is required")

    with session_scope() as session:
        proposal = session.get(CommandActionProposal, proposal_id)
        if proposal is None:
            raise ValueError(f"command action proposal not found: {proposal_id}")
        if proposal.status != "executed":
            raise RuntimeError(
                "command action proposal must be marked executed before "
                "workflow lifecycle can settle"
            )

        result = dict(proposal.result or {})
        expected_workflow_id = str(result.get("workflow_id") or "")
        if (
            expected_workflow_id
            and expected_workflow_id != normalized_workflow_id
        ):
            raise ValueError(
                "workflow lifecycle ID does not match the proposal result"
            )

        existing = _existing_event(
            session,
            proposal_id=proposal.id,
            event_type=event_type,
            workflow_id=normalized_workflow_id,
            cycle_key=cycle_key,
        )
        if existing is not None:
            return False

        payload = _correlation_payload(
            proposal,
            workflow_id=normalized_workflow_id,
        )
        started = _started_event(session, proposal.id)
        if started is not None:
            started_payload = dict(started.payload or {})
            for key in ("credential_id", "credential_fingerprint"):
                if started_payload.get(key) is not None:
                    payload[key] = started_payload[key]

        payload["state"] = normalized_state
        if cycle_key is not None:
            payload["cycle_key"] = cycle_key
        if detail:
            payload["detail"] = dict(detail)

        session.add(
            DomainEvent(
                aggregate_type="command_action_proposal",
                aggregate_id=str(proposal.id),
                event_type=event_type,
                payload=payload,
            )
        )
    return True


def record_intelligence_command_workflow_lifecycle(
    *,
    channel_profile_id: uuid.UUID,
    run_key: str,
    workflow_id: str,
    state: str,
    error: str | None = None,
) -> bool:
    proposal_id = proposal_id_from_command_run_key(run_key)
    if proposal_id is None:
        return False
    detail: dict[str, object] = {"run_key": run_key}
    if error:
        detail["error"] = error[:2000]

    with session_scope() as session:
        proposal = session.get(CommandActionProposal, proposal_id)
        if proposal is None:
            raise ValueError(f"command action proposal not found: {proposal_id}")
        if proposal.channel_profile_id != channel_profile_id:
            raise ValueError(
                "intelligence workflow channel does not match command proposal"
            )
        if proposal.action_type != "refresh_channel_intelligence":
            raise ValueError(
                "command run key does not reference an intelligence refresh action"
            )

    return record_command_workflow_lifecycle(
        proposal_id,
        workflow_id=workflow_id,
        state=state,
        detail=detail,
    )


def record_command_source_prepare_lifecycle(
    *,
    discovery_run_id: uuid.UUID,
    workflow_id: str,
    state: str,
    detail: dict[str, Any] | None = None,
) -> bool:
    with session_scope() as session:
        run = session.get(DiscoveryRun, discovery_run_id)
        if run is None:
            raise ValueError(f"discovery run not found: {discovery_run_id}")
        metadata = dict(run.run_metadata or {})
        raw_proposal_id = metadata.get("command_proposal_id")
        if not raw_proposal_id:
            return False
        try:
            proposal_id = uuid.UUID(str(raw_proposal_id))
        except ValueError as exc:
            raise ValueError(
                "discovery run has invalid command_proposal_id metadata"
            ) from exc
        proposal = session.get(CommandActionProposal, proposal_id)
        if proposal is None:
            raise ValueError(f"command action proposal not found: {proposal_id}")
        if proposal.action_type != "start_source_scout":
            raise ValueError(
                "discovery run command proposal is not a source-scout action"
            )
        expected_channel = str(metadata.get("command_channel_profile_id") or "")
        if expected_channel and expected_channel != str(proposal.channel_profile_id):
            raise ValueError(
                "discovery run channel does not match command proposal"
            )

    merged_detail: dict[str, object] = {
        "discovery_run_id": str(discovery_run_id),
    }
    if detail:
        merged_detail.update(detail)
    return record_command_workflow_lifecycle(
        proposal_id,
        workflow_id=workflow_id,
        state=state,
        detail=merged_detail,
    )


def record_topic_watch_command_cycle(
    *,
    topic_watch_id: uuid.UUID,
    workflow_id: str,
    cycle_key: str,
    state: str,
    detail: dict[str, Any] | None = None,
) -> bool:
    with session_scope() as session:
        watch = session.get(TopicWatchVersion, topic_watch_id)
        if watch is None:
            raise ValueError(f"topic watch version not found: {topic_watch_id}")
        metadata = dict(watch.watch_metadata or {})
        raw_proposal_id = metadata.get("command_proposal_id")
        if not raw_proposal_id:
            return False
        try:
            proposal_id = uuid.UUID(str(raw_proposal_id))
        except ValueError as exc:
            raise ValueError(
                "topic watch has invalid command_proposal_id metadata"
            ) from exc
        proposal = session.get(CommandActionProposal, proposal_id)
        if proposal is None:
            raise ValueError(f"command action proposal not found: {proposal_id}")
        if proposal.channel_profile_id != watch.channel_profile_id:
            raise ValueError(
                "topic watch channel does not match command proposal"
            )
        if proposal.action_type != "start_source_scout":
            raise ValueError(
                "topic watch command proposal is not a source-scout action"
            )

    merged_detail: dict[str, object] = {
        "topic_watch_id": str(topic_watch_id),
    }
    if detail:
        merged_detail.update(detail)
    return record_command_workflow_lifecycle(
        proposal_id,
        workflow_id=workflow_id,
        state=state,
        detail=merged_detail,
        cycle_key=cycle_key,
    )
