import uuid
from datetime import UTC, datetime, timedelta

from katcha.db import session_scope
from katcha.domain import ChannelStatus, ProductionStatus
from katcha.intelligence_models import ChannelProfile
from katcha.models import Clip, DomainEvent
from katcha.production_models import Production
from katcha.publishing_models import YouTubeConnection
from katcha.services.command_actions import (
    ActionProposalSpec,
    claim_action_proposal,
    complete_action_proposal,
    create_action_proposals,
)
from katcha.services.command_activity import get_action_activity
from katcha.services.intelligence_runs import (
    complete_channel_intelligence_run,
    start_channel_intelligence_run,
)


def _profile_and_production() -> tuple[uuid.UUID, Production]:
    connection_id = uuid.uuid4()
    profile_id = uuid.uuid4()
    clip_id = uuid.uuid4()
    production_id = uuid.uuid4()
    now = datetime.now(UTC)
    workflow_id = f"fixture-workflow-{production_id}"

    with session_scope() as session:
        session.add(
            YouTubeConnection(
                id=connection_id,
                channel_id=f"activity-{connection_id}",
                channel_title="Action activity fixture",
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
                profile_metadata={"channel_title": "Activity Fixture"},
            )
        )
        session.add(
            Clip(
                id=clip_id,
                sha256=uuid.uuid4().hex + uuid.uuid4().hex,
                storage_key=f"fixtures/{clip_id}.mp4",
                extension="mp4",
                status="scored",
                media_metadata={},
            )
        )
        production = Production(
            id=production_id,
            clip_id=clip_id,
            channel_profile_id=profile_id,
            workflow_id=workflow_id,
            kind="short",
            status=ProductionStatus.REVIEW.value,
            stage="review",
            persona_key="fixture",
            persona_version="1",
            prompt_version="fixture-v1",
            analysis_snapshot={},
            render_manifest={},
        )
        session.add(production)
        session.flush()
        session.add(
            DomainEvent(
                aggregate_type="production",
                aggregate_id=str(production.id),
                event_type="production.render_attempt_verified",
                payload={
                    "production_id": str(production.id),
                    "channel_profile_id": str(profile_id),
                },
            )
        )
        session.refresh(production)
        session.expunge(production)
    return profile_id, production


def test_action_activity_tracks_result_resource_to_review() -> None:
    profile_id, production = _profile_and_production()
    proposal = create_action_proposals(
        request_id=uuid.uuid4(),
        channel_profile_id=profile_id,
        specs=[
            ActionProposalSpec(
                action_type="create_short_production",
                label="Create production",
                description="Fixture action",
                payload={"clip_id": str(production.clip_id)},
            )
        ],
    )[0]
    claim_action_proposal(proposal.id, actor="control-token:fixture")
    complete_action_proposal(
        proposal.id,
        result={
            "production_id": str(production.id),
            "workflow_id": production.workflow_id,
        },
    )

    activity = get_action_activity(proposal.id)

    assert activity.proposal.status == "executed"
    assert activity.workflow_id == production.workflow_id
    assert activity.state == "awaiting_review"
    assert activity.settled is True
    assert activity.resource is not None
    assert activity.resource.kind == "production"
    assert activity.resource.id == production.id
    assert activity.resource.status == ProductionStatus.REVIEW.value
    assert activity.resource.stage == "review"
    event_types = [event["event_type"] for event in activity.events]
    assert "command_center.action_executed" in event_types
    assert "production.render_attempt_verified" in event_types


def test_action_activity_without_resource_reports_workflow_started() -> None:
    profile_id, _ = _profile_and_production()
    proposal = create_action_proposals(
        request_id=uuid.uuid4(),
        channel_profile_id=profile_id,
        specs=[
            ActionProposalSpec(
                action_type="refresh_channel_intelligence",
                label="Refresh intelligence",
                description="Fixture refresh",
                payload={},
            )
        ],
    )[0]
    claim_action_proposal(proposal.id, actor="control-token:fixture")
    complete_action_proposal(
        proposal.id,
        result={
            "workflow_id": "fixture-intelligence-workflow",
            "run_key": "fixture-run",
        },
    )

    activity = get_action_activity(proposal.id)

    assert activity.resource is None
    assert activity.workflow_id == "fixture-intelligence-workflow"
    assert activity.state == "workflow_started"
    assert activity.settled is False



def test_action_activity_tracks_completed_intelligence_refresh() -> None:
    profile_id, _ = _profile_and_production()
    workflow_id = f"fixture-intelligence-{uuid.uuid4()}"
    run_key = f"command-fixture-{uuid.uuid4()}"
    proposal = create_action_proposals(
        request_id=uuid.uuid4(),
        channel_profile_id=profile_id,
        specs=[
            ActionProposalSpec(
                action_type="refresh_channel_intelligence",
                label="Refresh intelligence",
                description="Fixture refresh",
                payload={},
            )
        ],
    )[0]
    claim_action_proposal(proposal.id, actor="control-token:fixture")
    complete_action_proposal(
        proposal.id,
        result={
            "workflow_id": workflow_id,
            "run_key": run_key,
        },
    )
    start_channel_intelligence_run(
        profile_id,
        run_key=run_key,
        workflow_id=workflow_id,
    )
    complete_channel_intelligence_run(
        profile_id,
        run_key=run_key,
        workflow_id=workflow_id,
        result={
            "ranking": {
                "ranking_version": 8,
                "sample_count": 41,
                "confidence": 0.88,
            }
        },
    )

    activity = get_action_activity(proposal.id)

    assert activity.resource is not None
    assert activity.resource.kind == "channel_intelligence_run"
    assert activity.resource.status == "completed"
    assert activity.resource.stage == "completed"
    assert activity.state == "completed"
    assert activity.settled is True
    event_types = [event["event_type"] for event in activity.events]
    assert "channel_intelligence.run_started" in event_types
    assert "channel_intelligence.run_completed" in event_types
