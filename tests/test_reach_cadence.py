from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import katcha.db as db
import katcha.orchestration.intelligence_activities as activities
import katcha.orchestration.intelligence_workflows as workflows
from katcha.integrations.youtube.reporting import ANALYTICS_SCOPE
from katcha.intelligence_models import ChannelProfile
from katcha.publishing_models import YouTubeConnection
from katcha.services.reach_cadence import eligible_reach_connection, reach_sync_identity


def test_pacific_identity_reused_across_utc_date_and_changes_at_local_midnight():
    connection = uuid.uuid4()
    before = datetime(2026, 9, 25, 6, 59, tzinfo=UTC)  # 23:59 PDT
    after = datetime(2026, 9, 25, 7, 1, tzinfo=UTC)
    assert reach_sync_identity(connection, before) == reach_sync_identity(
        connection, datetime(2026, 9, 24, 19, tzinfo=UTC)
    )
    assert reach_sync_identity(connection, before) != reach_sync_identity(connection, after)
    with pytest.raises(ValueError):
        reach_sync_identity(connection, datetime(2026, 9, 24))


@pytest.mark.asyncio
async def test_cadence_skips_missing_scope_and_reuses_workflow_identity(monkeypatch):
    engine = create_engine(
        "sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    db.Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(db, "SessionLocal", factory)
    channel_id, connection_id = uuid.uuid4(), uuid.uuid4()
    with factory.begin() as session:
        session.add(
            YouTubeConnection(
                id=connection_id,
                channel_id="fixture",
                channel_title="Fixture",
                status="active",
                scopes=[],
                encrypted_access_token="fixture",
                encrypted_refresh_token="fixture",
                token_expires_at=datetime.now(UTC),
                connection_metadata={},
            )
        )
        session.add(
            ChannelProfile(id=channel_id, youtube_connection_id=connection_id, status="active")
        )
    assert eligible_reach_connection(channel_id) == (None, "analytics_scope_missing")
    calls = []

    async def fake_start(connection, workflow_id):
        calls.append((connection, workflow_id))
        return workflow_id

    monkeypatch.setattr(activities, "start_reach_sync_workflow", fake_start)
    before = datetime(2026, 9, 25, 6, 59, tzinfo=UTC)
    skipped = await activities.schedule_channel_reach_sync_activity(
        str(channel_id), before.isoformat()
    )
    assert skipped == {"status": "skipped", "reason": "analytics_scope_missing"}
    assert not calls
    with factory.begin() as session:
        connection = session.get(YouTubeConnection, connection_id)
        connection.scopes = [ANALYTICS_SCOPE]
    for _ in range(2):
        scheduled = await activities.schedule_channel_reach_sync_activity(
            str(channel_id), before.isoformat()
        )
    assert scheduled["status"] == "scheduled"
    assert len(calls) == 2
    assert calls[0] == calls[1] == (str(connection_id), scheduled["workflow_id"])
    with factory.begin() as session:
        session.get(ChannelProfile, channel_id).status = "paused"
    assert eligible_reach_connection(channel_id) == (None, "channel_inactive")
    engine.dispose()


@pytest.mark.asyncio
async def test_reach_scheduling_failure_does_not_block_intelligence(monkeypatch):
    calls = []

    async def fake_activity(name, *args, **kwargs):
        calls.append(name)
        if name == "schedule_channel_reach_sync_activity":
            raise RuntimeError("publishing queue unavailable")
        return {"activity": name}

    monkeypatch.setattr(workflows.workflow, "execute_activity", fake_activity)
    monkeypatch.setattr(workflows.workflow, "now", lambda: datetime(2026, 9, 24, tzinfo=UTC))
    result = await workflows._run_refresh(str(uuid.uuid4()), "scheduled-cycle")
    assert result["reach_sync"] == {
        "status": "unavailable",
        "reason": "reach_sync_scheduling_failed",
    }
    assert result["packaging_intelligence"]["activity"] == (
        "refresh_packaging_intelligence_activity"
    )
    assert calls[-1] == "apply_channel_safety_demotion_activity"
