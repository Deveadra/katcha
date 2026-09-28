import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from katcha.db import session_scope
from katcha.domain import ChannelStatus
from katcha.intelligence_models import ChannelProfile
from katcha.models import DomainEvent
from katcha.publishing_models import YouTubeConnection
from katcha.services.command_actions import (
    ActionProposalSpec,
    claim_action_proposal,
    complete_action_proposal,
    create_action_proposals,
)
from katcha.services.command_history import (
    archive_command_thread,
    create_command_thread,
    get_command_thread,
    list_command_threads,
    list_command_turns,
    record_command_exchange,
)


def _channel_profile() -> uuid.UUID:
    connection_id = uuid.uuid4()
    profile_id = uuid.uuid4()
    now = datetime.now(UTC)
    with session_scope() as session:
        session.add(
            YouTubeConnection(
                id=connection_id,
                channel_id=f"fixture-{connection_id}",
                channel_title="Katcha AI history fixture",
                status="active",
                scopes=[],
                encrypted_access_token="fixture",
                encrypted_refresh_token="fixture",
                token_expires_at=now + timedelta(hours=1),
                connection_metadata={},
            )
        )
        session.flush()
        session.add(
            ChannelProfile(
                id=profile_id,
                youtube_connection_id=connection_id,
                status=ChannelStatus.ACTIVE.value,
                timezone="America/Chicago",
                active_strategy_version=1,
                active_automation_version=1,
                profile_metadata={"channel_title": "History Fixture"},
            )
        )
    return profile_id


def test_command_thread_persists_atomic_exchange_and_archive() -> None:
    profile_id = _channel_profile()
    request_id = uuid.uuid4()
    thread = create_command_thread(
        channel_profile_id=profile_id,
        actor="control-token:fixture",
        title="  What is currently failing in RankSnaxx?  ",
    )

    user_turn, assistant_turn = record_command_exchange(
        thread_id=thread.id,
        request_id=request_id,
        user_content="What is currently failing?",
        assistant_content="One current render failure is unresolved.",
        intent="failures",
        narrator="fixture/grounded-command-v1",
        evidence=[{"kind": "render_attempt", "id": "render-fixture"}],
        user_context={"selected_clip_ids": []},
        assistant_context={"key_points": ["Newest generation only."]},
    )

    assert user_turn.sequence_number == 1
    assert assistant_turn.sequence_number == 2
    assert user_turn.request_id == request_id
    assert assistant_turn.request_id == request_id
    assert assistant_turn.evidence == [
        {"kind": "render_attempt", "id": "render-fixture"}
    ]

    loaded = get_command_thread(thread.id, channel_profile_id=profile_id)
    assert loaded.title == "What is currently failing in RankSnaxx?"
    assert loaded.status == "active"

    turns = list_command_turns(thread.id)
    assert [turn.role for turn in turns] == ["user", "assistant"]
    assert [turn.sequence_number for turn in turns] == [1, 2]

    active = list_command_threads(profile_id)
    assert any(row.id == thread.id for row in active)

    archived = archive_command_thread(
        thread.id,
        actor="control-token:fixture",
    )
    assert archived.status == "archived"
    assert all(row.id != thread.id for row in list_command_threads(profile_id))

    with pytest.raises(ValueError, match="different channel"):
        get_command_thread(thread.id, channel_profile_id=uuid.uuid4())


def test_proposal_events_carry_thread_turn_and_workflow_correlation() -> None:
    profile_id = _channel_profile()
    request_id = uuid.uuid4()
    thread = create_command_thread(
        channel_profile_id=profile_id,
        actor="control-token:fixture",
        title="Create a clip",
    )
    _, assistant_turn = record_command_exchange(
        thread_id=thread.id,
        request_id=request_id,
        user_content="Create a clip.",
        assistant_content="I prepared a proposal.",
        intent="create_content",
        narrator="fixture/grounded-command-v1",
    )

    proposal = create_action_proposals(
        request_id=request_id,
        channel_profile_id=profile_id,
        thread_id=thread.id,
        source_turn_id=assistant_turn.id,
        specs=[
            ActionProposalSpec(
                action_type="refresh_channel_intelligence",
                label="Refresh",
                description="Refresh channel intelligence",
                payload={},
            )
        ],
    )[0]
    claim_action_proposal(proposal.id, actor="control-token:fixture")
    complete_action_proposal(
        proposal.id,
        result={
            "workflow_id": "workflow-fixture",
            "run_key": "run-fixture",
        },
    )

    with session_scope() as session:
        events = list(
            session.scalars(
                select(DomainEvent)
                .where(DomainEvent.aggregate_id == str(proposal.id))
                .order_by(DomainEvent.created_at, DomainEvent.id)
            )
        )

    by_type = {event.event_type: dict(event.payload or {}) for event in events}
    assert "command_center.proposal_created" in by_type
    assert "command_center.action_confirmed" in by_type
    assert "command_center.action_executed" in by_type
    assert "command_center.workflow_started" in by_type

    started = by_type["command_center.workflow_started"]
    assert started["proposal_id"] == str(proposal.id)
    assert started["request_id"] == str(request_id)
    assert started["thread_id"] == str(thread.id)
    assert started["source_turn_id"] == str(assistant_turn.id)
    assert started["channel_profile_id"] == str(profile_id)
    assert started["workflow_id"] == "workflow-fixture"



def test_action_proposal_rejects_mismatched_conversation_authority() -> None:
    profile_id = _channel_profile()
    request_id = uuid.uuid4()
    thread = create_command_thread(
        channel_profile_id=profile_id,
        actor="control-token:fixture",
        title="Authority fixture",
    )
    _, assistant_turn = record_command_exchange(
        thread_id=thread.id,
        request_id=request_id,
        user_content="Prepare an action.",
        assistant_content="I prepared a proposal.",
        intent="create_content",
        narrator="fixture/grounded-command-v1",
    )

    with pytest.raises(ValueError, match="does not match the request thread"):
        create_action_proposals(
            request_id=uuid.uuid4(),
            channel_profile_id=profile_id,
            thread_id=thread.id,
            source_turn_id=assistant_turn.id,
            specs=[
                ActionProposalSpec(
                    action_type="refresh_channel_intelligence",
                    label="Refresh",
                    description="Fixture",
                    payload={},
                )
            ],
        )

    with pytest.raises(ValueError, match="must be supplied together"):
        create_action_proposals(
            request_id=request_id,
            channel_profile_id=profile_id,
            thread_id=thread.id,
            specs=[],
        )
