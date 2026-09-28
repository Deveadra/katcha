import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from katcha.db import session_scope
from katcha.domain import ChannelStatus
from katcha.intelligence_models import ChannelProfile
from katcha.models import DomainEvent
from katcha.publishing_models import YouTubeConnection
from katcha.services.intelligence_runs import (
    complete_channel_intelligence_run,
    fail_channel_intelligence_run,
    get_channel_intelligence_run,
    list_channel_intelligence_runs,
    start_channel_intelligence_run,
)


def _channel_profile() -> uuid.UUID:
    connection_id = uuid.uuid4()
    profile_id = uuid.uuid4()
    now = datetime.now(UTC)
    with session_scope() as session:
        session.add(
            YouTubeConnection(
                id=connection_id,
                channel_id=f"intelligence-run-{connection_id}",
                channel_title="Intelligence run fixture",
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
                profile_metadata={"channel_title": "Intelligence Run Fixture"},
            )
        )
    return profile_id


def test_intelligence_run_lifecycle_is_durable_and_idempotent() -> None:
    profile_id = _channel_profile()
    run_key = f"fixture-{uuid.uuid4().hex}"
    workflow_id = f"channel-intelligence-refresh-{profile_id}-{uuid.uuid4().hex[:12]}"

    started = start_channel_intelligence_run(
        profile_id,
        run_key=run_key,
        workflow_id=workflow_id,
    )
    replay = start_channel_intelligence_run(
        profile_id,
        run_key=run_key,
        workflow_id=workflow_id,
    )

    assert replay.id == started.id
    assert replay.status == "running"

    completed = complete_channel_intelligence_run(
        profile_id,
        run_key=run_key,
        workflow_id=workflow_id,
        result={
            "channel_profile_id": str(profile_id),
            "run_key": run_key,
            "ranking": {
                "ranking_snapshot_id": str(uuid.uuid4()),
                "ranking_version": 4,
                "sample_count": 32,
                "confidence": 0.82,
                "coefficients": {"should": "not be copied"},
            },
            "packaging_intelligence": {
                "snapshot_id": str(uuid.uuid4()),
                "recommendation_count": 8,
                "recommendations": [{"large": "payload"}] * 50,
            },
        },
    )

    assert completed.status == "completed"
    assert completed.stage == "completed"
    assert completed.completed_at is not None
    assert "coefficients" not in completed.result_summary["ranking"]
    assert (
        "recommendations"
        not in completed.result_summary["packaging_intelligence"]
    )

    completed_replay = complete_channel_intelligence_run(
        profile_id,
        run_key=run_key,
        workflow_id=workflow_id,
        result={"ranking": {"ranking_version": 99}},
    )
    assert completed_replay.id == completed.id
    assert completed_replay.result_summary == completed.result_summary

    loaded = get_channel_intelligence_run(profile_id, run_key)
    assert loaded.id == completed.id
    assert any(
        row.id == completed.id
        for row in list_channel_intelligence_runs(profile_id)
    )

    with session_scope() as session:
        events = list(
            session.scalars(
                select(DomainEvent)
                .where(
                    DomainEvent.aggregate_type == "channel_intelligence_run",
                    DomainEvent.aggregate_id == str(completed.id),
                )
                .order_by(DomainEvent.created_at, DomainEvent.id)
            )
        )
    event_types = [event.event_type for event in events]
    assert event_types.count("channel_intelligence.run_started") == 1
    assert event_types.count("channel_intelligence.run_completed") == 1
    assert all(
        event.payload["channel_profile_id"] == str(profile_id)
        for event in events
    )


def test_intelligence_run_failure_and_workflow_binding_are_authoritative() -> None:
    profile_id = _channel_profile()
    run_key = f"failure-{uuid.uuid4().hex}"
    workflow_id = f"workflow-{uuid.uuid4()}"

    failed = fail_channel_intelligence_run(
        profile_id,
        run_key=run_key,
        workflow_id=workflow_id,
        error="Activity task failed: fixture root cause",
    )

    assert failed.status == "failed"
    assert failed.stage == "failed"
    assert failed.completed_at is not None
    assert failed.error == "Activity task failed: fixture root cause"

    restarted = start_channel_intelligence_run(
        profile_id,
        run_key=run_key,
        workflow_id=workflow_id,
    )
    assert restarted.id == failed.id
    assert restarted.status == "running"
    assert restarted.error is None

    with pytest.raises(ValueError, match="different workflow"):
        start_channel_intelligence_run(
            profile_id,
            run_key=run_key,
            workflow_id=f"different-{uuid.uuid4()}",
        )


def test_completed_intelligence_run_cannot_regress_to_failed() -> None:
    profile_id = _channel_profile()
    run_key = f"complete-{uuid.uuid4().hex}"
    workflow_id = f"workflow-{uuid.uuid4()}"

    complete_channel_intelligence_run(
        profile_id,
        run_key=run_key,
        workflow_id=workflow_id,
        result={"ranking": {"ranking_version": 1}},
    )
    unchanged = fail_channel_intelligence_run(
        profile_id,
        run_key=run_key,
        workflow_id=workflow_id,
        error="late duplicate failure",
    )

    assert unchanged.status == "completed"
    assert unchanged.error is None
