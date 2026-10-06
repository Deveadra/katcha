from datetime import UTC, datetime, timedelta

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from starlette.requests import Request

from katcha.api.control_auth import control_actor, require_control_scope
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
    get_action_proposal,
    reject_action_proposal,
)


def _request() -> Request:
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/v1/ai/actions/test/execute",
            "headers": [],
        }
    )


def test_control_actor_and_scopes_are_server_derived() -> None:
    request = _request()
    request.state.control_actor = "control-token:fixture"
    request.state.control_scopes = {"ai:read", "production:create"}

    assert control_actor(request) == "control-token:fixture"
    require_control_scope(request, "ai:read")
    require_control_scope(request, "production:create")

    with pytest.raises(HTTPException) as exc:
        require_control_scope(request, "render:recover")
    assert exc.value.status_code == 403


def test_action_proposal_claim_is_idempotent() -> None:
    connection_id = __import__("uuid").uuid4()
    profile_id = __import__("uuid").uuid4()
    request_id = __import__("uuid").uuid4()
    now = datetime.now(UTC)

    with session_scope() as session:
        session.add(
            YouTubeConnection(
                id=connection_id,
                channel_id=f"fixture-{connection_id}",
                channel_title="Fixture channel",
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
                timezone="UTC",
                active_strategy_version=1,
                active_automation_version=1,
                profile_metadata={"channel_title": "Fixture channel"},
            )
        )

    proposals = create_action_proposals(
        request_id=request_id,
        channel_profile_id=profile_id,
        specs=[
            ActionProposalSpec(
                action_type="refresh_channel_intelligence",
                label="Refresh",
                description="Fixture refresh",
                payload={"fixture": True},
            )
        ],
    )
    assert len(proposals) == 1
    proposal = proposals[0]
    assert proposal.status == "proposed"
    assert proposal.idempotency_key == f"command-proposal:{proposal.id}"

    first = claim_action_proposal(
        proposal.id,
        actor="control-principal:aerith",
        credential_id="2026-q4",
        credential_fingerprint="abc123def456",
    )
    assert first.should_execute is True
    assert first.proposal.status == "executing"
    assert first.proposal.execution_attempts == 1
    assert first.proposal.confirmed_by == "control-principal:aerith"

    duplicate = claim_action_proposal(
        proposal.id,
        actor="control-principal:aerith",
        credential_id="2026-q4",
        credential_fingerprint="abc123def456",
    )
    assert duplicate.should_execute is False
    assert duplicate.proposal.status == "executing"
    assert duplicate.proposal.execution_attempts == 1

    complete_action_proposal(
        proposal.id,
        result={"workflow_id": "fixture-workflow"},
        credential_id="2026-q4",
        credential_fingerprint="abc123def456",
    )
    replay = claim_action_proposal(
        proposal.id,
        actor="control-principal:aerith",
        credential_id="2026-q4",
        credential_fingerprint="abc123def456",
    )
    assert replay.should_execute is False
    assert replay.proposal.status == "executed"
    assert replay.proposal.result == {"workflow_id": "fixture-workflow"}

    loaded = get_action_proposal(proposal.id)
    assert loaded.status == "executed"
    assert loaded.execution_attempts == 1

    with session_scope() as session:
        events = list(
            session.scalars(
                select(DomainEvent)
                .where(DomainEvent.aggregate_id == str(proposal.id))
                .order_by(DomainEvent.created_at, DomainEvent.id)
            )
        )

    audited = [
        event
        for event in events
        if event.event_type
        in {
            "command_center.action_confirmed",
            "command_center.action_executed",
            "command_center.workflow_started",
        }
    ]
    assert audited
    assert all(
        (event.payload or {}).get("credential_id") == "2026-q4"
        for event in audited
    )
    assert all(
        (event.payload or {}).get("credential_fingerprint")
        == "abc123def456"
        for event in audited
    )


def test_action_proposal_rejection_is_audited_and_terminal() -> None:
    connection_id = __import__("uuid").uuid4()
    profile_id = __import__("uuid").uuid4()
    request_id = __import__("uuid").uuid4()
    now = datetime.now(UTC)

    with session_scope() as session:
        session.add(
            YouTubeConnection(
                id=connection_id,
                channel_id=f"fixture-{connection_id}",
                channel_title="Fixture channel",
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
                timezone="UTC",
                active_strategy_version=1,
                active_automation_version=1,
                profile_metadata={"channel_title": "Fixture channel"},
            )
        )

    proposal = create_action_proposals(
        request_id=request_id,
        channel_profile_id=profile_id,
        specs=[
            ActionProposalSpec(
                action_type="editorial_storyboard_edit",
                label="Apply Storyboard edit",
                description="Fixture edit",
                payload={"project_id": str(__import__("uuid").uuid4())},
            )
        ],
    )[0]

    rejected = reject_action_proposal(
        proposal.id,
        actor="control-principal:editor",
        reason="Use a different shot.",
        credential_id="editor-key",
        credential_fingerprint="fingerprint",
    )
    assert rejected.status == "rejected"
    assert rejected.result == {
        "rejected": True,
        "reason": "Use a different shot.",
    }

    replay = reject_action_proposal(
        proposal.id,
        actor="control-principal:editor",
        reason="Use a different shot.",
    )
    assert replay.status == "rejected"

    with pytest.raises(ValueError, match="cannot execute from status rejected"):
        claim_action_proposal(
            proposal.id,
            actor="control-principal:editor",
        )

    with session_scope() as session:
        event = session.scalar(
            select(DomainEvent).where(
                DomainEvent.aggregate_id == str(proposal.id),
                DomainEvent.event_type == "command_center.proposal_rejected",
            )
        )
    assert event is not None
    assert event.payload["actor"] == "control-principal:editor"
    assert event.payload["credential_id"] == "editor-key"
