import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier

import pytest
from sqlalchemy import select

from katcha.db import session_scope
from katcha.domain import ChannelStatus
from katcha.intelligence_models import ChannelProfile
from katcha.models import DomainEvent
from katcha.publishing_models import YouTubeConnection
from katcha.services.acquisition import register_discovery_run
from katcha.services.command_actions import (
    ActionProposalSpec,
    claim_action_proposal,
    complete_action_proposal,
    create_action_proposals,
)
from katcha.services.command_activity import get_action_activity
from katcha.services.command_workflow_lifecycle import (
    proposal_id_from_command_run_key,
    record_command_source_prepare_lifecycle,
    record_command_workflow_lifecycle,
    record_intelligence_command_workflow_lifecycle,
    record_topic_watch_command_cycle,
)
from katcha.services.discovery_trends import create_topic_watch_version


def _profile() -> uuid.UUID:
    connection_id = uuid.uuid4()
    profile_id = uuid.uuid4()
    now = datetime.now(UTC)
    with session_scope() as session:
        session.add(
            YouTubeConnection(
                id=connection_id,
                channel_id=f"workflow-lifecycle-{connection_id}",
                channel_title="Workflow lifecycle fixture",
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
                profile_metadata={"channel_title": "Workflow lifecycle"},
            )
        )
    return profile_id


def _executed_proposal(
    profile_id: uuid.UUID,
    *,
    action_type: str,
    result: dict[str, object],
) -> object:
    proposal = create_action_proposals(
        request_id=uuid.uuid4(),
        channel_profile_id=profile_id,
        specs=[
            ActionProposalSpec(
                action_type=action_type,
                label="Fixture action",
                description="Workflow lifecycle fixture",
                payload={},
            )
        ],
    )[0]
    claim_action_proposal(
        proposal.id,
        actor="control-principal:aerith",
        credential_id="2026-q4",
        credential_fingerprint="abc123def456",
    )
    return complete_action_proposal(
        proposal.id,
        result=result,
        credential_id="2026-q4",
        credential_fingerprint="abc123def456",
    )


def test_intelligence_command_workflow_completion_is_idempotent() -> None:
    profile_id = _profile()
    proposal_id = uuid.uuid4()
    workflow_id = f"intelligence-fixture-{proposal_id}"
    proposal = create_action_proposals(
        request_id=uuid.uuid4(),
        channel_profile_id=profile_id,
        specs=[
            ActionProposalSpec(
                action_type="refresh_channel_intelligence",
                label="Refresh",
                description="Refresh fixture",
                payload={},
            )
        ],
    )[0]
    proposal_id = proposal.id
    claim_action_proposal(proposal.id, actor="control-principal:aerith")
    complete_action_proposal(
        proposal.id,
        result={
            "workflow_id": workflow_id,
            "run_key": f"command-proposal-{proposal.id}",
        },
    )

    run_key = f"command-proposal-{proposal.id}"
    assert proposal_id_from_command_run_key(run_key) == proposal.id
    assert (
        record_intelligence_command_workflow_lifecycle(
            channel_profile_id=profile_id,
            run_key=run_key,
            workflow_id=workflow_id,
            state="completed",
        )
        is True
    )
    assert (
        record_intelligence_command_workflow_lifecycle(
            channel_profile_id=profile_id,
            run_key=run_key,
            workflow_id=workflow_id,
            state="completed",
        )
        is False
    )

    activity = get_action_activity(proposal.id)
    assert activity.state == "completed"
    assert activity.settled is True
    assert activity.resource is None

    with session_scope() as session:
        events = list(
            session.scalars(
                select(DomainEvent).where(
                    DomainEvent.aggregate_id == str(proposal.id),
                    DomainEvent.event_type
                    == "command_center.workflow_completed",
                )
            )
        )
    assert len(events) == 1
    payload = dict(events[0].payload or {})
    assert payload["request_id"] == str(proposal.request_id)
    assert payload["channel_profile_id"] == str(profile_id)
    assert payload["action_type"] == "refresh_channel_intelligence"
    assert payload["workflow_id"] == workflow_id


def test_non_command_intelligence_run_does_not_emit_lifecycle() -> None:
    assert proposal_id_from_command_run_key("scheduled-20260929T120000Z") is None
    assert (
        record_intelligence_command_workflow_lifecycle(
            channel_profile_id=uuid.uuid4(),
            run_key="scheduled-20260929T120000Z",
            workflow_id="scheduled-fixture",
            state="completed",
        )
        is False
    )


def test_concurrent_lifecycle_replay_writes_one_receipt() -> None:
    workflow_id = f"concurrent-{uuid.uuid4()}"
    proposal = _executed_proposal(
        _profile(), action_type="start_source_scout",
        result={"workflow_id": workflow_id},
    )
    barrier = Barrier(2)

    def record() -> bool:
        barrier.wait(timeout=10)
        return record_command_workflow_lifecycle(
            proposal.id, workflow_id=workflow_id, state="completed",
            cycle_key="same-cycle",
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(record) for _ in range(2)]
        assert sorted(future.result(timeout=20) for future in futures) == [False, True]


@pytest.mark.parametrize("clock_step", [0, -60])
def test_source_scout_cycles_report_active_degraded_then_recovered(
    monkeypatch: pytest.MonkeyPatch, clock_step: int,
) -> None:
    from katcha.services import command_workflow_lifecycle as lifecycle

    class Clock(datetime):
        current = datetime(2040, 1, 1, tzinfo=UTC)

        @classmethod
        def now(cls, tz=None):
            return cls.current

    monkeypatch.setattr(lifecycle, "datetime", Clock)
    profile_id = _profile()
    proposal = create_action_proposals(
        request_id=uuid.uuid4(),
        channel_profile_id=profile_id,
        specs=[
            ActionProposalSpec(
                action_type="start_source_scout",
                label="Scout",
                description="Source scout fixture",
                payload={},
            )
        ],
    )[0]
    watch = create_topic_watch_version(
        watch_key=f"fixture-{proposal.id}",
        name="Command source scout fixture",
        include_terms=["xbox"],
        adapter_configs=[],
        metadata={"command_proposal_id": str(proposal.id)},
        channel_profile_id=profile_id,
    )
    workflow_id = f"topic-watch-schedule-{watch.id}"
    claim_action_proposal(proposal.id, actor="control-principal:aerith")
    complete_action_proposal(
        proposal.id,
        result={
            "topic_watch_id": str(watch.id),
            "workflow_id": workflow_id,
            "continuous": True,
        },
    )

    assert record_topic_watch_command_cycle(
        topic_watch_id=watch.id,
        workflow_id=workflow_id,
        cycle_key="cycle-1",
        state="failed",
        detail={
            "cycle_workflow_id": "child-1",
            "error": "provider fixture failure",
        },
    )
    degraded = get_action_activity(proposal.id)
    assert degraded.state == "active_degraded"
    assert degraded.settled is False
    assert degraded.resource is not None
    assert degraded.resource.kind == "topic_watch"
    assert degraded.resource.stage == "cycle_failed"
    assert degraded.resource.error == "provider fixture failure"

    Clock.current += timedelta(seconds=clock_step)
    assert record_topic_watch_command_cycle(
        topic_watch_id=watch.id,
        workflow_id=workflow_id,
        cycle_key="cycle-2",
        state="completed",
        detail={
            "cycle_workflow_id": "child-2",
            "queue_status": "completed",
            "queue_count": 7,
        },
    )
    recovered = get_action_activity(proposal.id)
    assert recovered.state == "active"
    assert recovered.settled is False
    assert recovered.resource is not None
    assert recovered.resource.stage == "completed"
    assert recovered.resource.error is None

    duplicate = record_topic_watch_command_cycle(
        topic_watch_id=watch.id,
        workflow_id=workflow_id,
        cycle_key="cycle-2",
        state="completed",
        detail={"queue_status": "completed"},
    )
    assert duplicate is False

    with session_scope() as session:
        events = list(session.scalars(select(DomainEvent).where(
            DomainEvent.aggregate_id == str(proposal.id),
            DomainEvent.event_type.in_([
                "command_center.workflow_cycle_failed",
                "command_center.workflow_cycle_completed",
            ]),
        ).order_by(DomainEvent.created_at)))
        assert len(events) == 2
        assert events[0].created_at < events[1].created_at
        assert events[1].payload["cycle_key"] == "cycle-2"
        row = session.get(type(watch), watch.id)
        assert row is not None
        row.enabled = False

    disabled = get_action_activity(proposal.id)
    assert disabled.state == "disabled"
    assert disabled.settled is True


def test_command_source_prepare_workflow_completion_is_visible() -> None:
    profile_id = _profile()
    proposal = create_action_proposals(
        request_id=uuid.uuid4(),
        channel_profile_id=profile_id,
        specs=[
            ActionProposalSpec(
                action_type="start_source_scout",
                label="Search Marvel Entertainment",
                description="Find VisionQuest trailers and prepare them.",
                payload={"prepare_for_production": True},
            )
        ],
    )[0]
    run = register_discovery_run(
        adapter_key="manifest",
        adapter_version="v1",
        query={"items": []},
        metadata={
            "command_proposal_id": str(proposal.id),
            "command_channel_profile_id": str(profile_id),
            "command_prepare_for_production": True,
        },
    )
    workflow_id = f"command-source-prepare-{run.id}"
    claim_action_proposal(proposal.id, actor="control-principal:operator")
    complete_action_proposal(
        proposal.id,
        result={
            "workflow_id": workflow_id,
            "discovery_run_id": str(run.id),
            "prepare_for_production": True,
        },
    )

    assert record_command_source_prepare_lifecycle(
        discovery_run_id=run.id,
        workflow_id=workflow_id,
        state="completed",
        detail={
            "candidate_count": 2,
            "prepared_count": 2,
            "ingest_count": 2,
            "failed_ingest_count": 0,
        },
    )

    activity = get_action_activity(proposal.id)
    assert activity.state == "completed"
    assert activity.settled is True
    assert any(
        event["event_type"] == "command_center.workflow_completed"
        for event in activity.events
    )


def test_intelligence_workflow_failure_is_terminal() -> None:
    profile_id = _profile()
    workflow_id = f"intelligence-failure-{uuid.uuid4()}"
    proposal = create_action_proposals(
        request_id=uuid.uuid4(),
        channel_profile_id=profile_id,
        specs=[
            ActionProposalSpec(
                action_type="refresh_channel_intelligence",
                label="Refresh",
                description="Failure fixture",
                payload={},
            )
        ],
    )[0]
    claim_action_proposal(proposal.id, actor="control-principal:aerith")
    complete_action_proposal(
        proposal.id,
        result={
            "workflow_id": workflow_id,
            "run_key": f"command-proposal-{proposal.id}",
        },
    )

    record_intelligence_command_workflow_lifecycle(
        channel_profile_id=profile_id,
        run_key=f"command-proposal-{proposal.id}",
        workflow_id=workflow_id,
        state="failed",
        error="fixture refresh failed",
    )

    activity = get_action_activity(proposal.id)
    assert activity.state == "failed"
    assert activity.settled is True
