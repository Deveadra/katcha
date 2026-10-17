"""Regression proof that a Temporal cancellation request cannot unlock clip deletion."""
import uuid
from types import SimpleNamespace

import pytest
from temporalio.client import WorkflowExecutionStatus

from katcha import db
from katcha.models import DomainEvent
from katcha.production_models import Production
from katcha.services import clip_work_resolution as resolution
from katcha.services.clip_lifecycle import active_clip_references
from katcha.short_episode_models import ShortEpisode


class FakeHandle:
    def __init__(self, status):
        self.status = status
        self.cancel_count = 0

    async def describe(self):
        return SimpleNamespace(status=self.status)

    async def cancel(self):
        self.cancel_count += 1


class FakeTemporal:
    def __init__(self, entries):
        self.handles = {name: FakeHandle(status) for name, status in entries.items()}

    def get_workflow_handle(self, workflow_id):
        return self.handles[workflow_id]


@pytest.mark.asyncio
async def test_running_temporal_request_does_not_release_media(fixture_work, monkeypatch):
    clip_id, _, _, ids = fixture_work
    client = FakeTemporal({"purge-prod": WorkflowExecutionStatus.RUNNING})

    async def get_client():
        return client

    monkeypatch.setattr(resolution, "get_temporal_client", get_client)
    pending = await resolution.stop_clip_work(
        clip_id, "production", ids["production"], expected_status="review",
        actor="test-operator",
    )
    assert pending.state == "cancellation_requested"
    assert client.handles["purge-prod"].cancel_count == 1
    with db.session_scope() as session:
        assert session.get(Production, ids["production"]).status == "review"
        assert len(active_clip_references(session, clip_id)) == 3

    client.handles["purge-prod"].status = WorkflowExecutionStatus.CANCELED
    finished = await resolution.stop_clip_work(
        clip_id, "production", ids["production"], expected_status="review",
        actor="test-operator",
    )
    assert finished.state == "resolved"
    assert finished.status == "cancelled"
    with db.session_scope() as session:
        assert session.get(Production, ids["production"]).status == "cancelled"
        assert len(active_clip_references(session, clip_id)) == 2
        audit = session.query(DomainEvent).filter_by(
            aggregate_id=str(ids["production"]),
            event_type="production.operator_cancelled",
        ).one()
        assert audit.payload["verified_temporal_terminal"] == ["CANCELED"]
        assert audit.payload["clip_id"] == str(clip_id)
    repeat = await resolution.stop_clip_work(
        clip_id, "production", ids["production"], expected_status="review",
        actor="test-operator",
    )
    assert repeat.state == "already_resolved"


@pytest.mark.asyncio
async def test_review_rejection_requires_completed_workflow(fixture_work, monkeypatch):
    clip_id, _, _, ids = fixture_work
    client = FakeTemporal({"purge-prod": WorkflowExecutionStatus.COMPLETED})

    async def get_client():
        return client

    monkeypatch.setattr(resolution, "get_temporal_client", get_client)
    finished = await resolution.stop_clip_work(
        clip_id, "production", ids["production"], expected_status="review",
        actor="test-operator",
    )
    assert finished.state == "resolved"
    assert finished.status == "rejected"


@pytest.mark.asyncio
async def test_queued_record_with_completed_workflow_is_not_silently_erased(
    fixture_work, monkeypatch,
):
    clip_id, _, _, ids = fixture_work
    with db.session_scope() as session:
        session.get(Production, ids["production"]).status = "queued"
    client = FakeTemporal({"purge-prod": WorkflowExecutionStatus.COMPLETED})

    async def get_client():
        return client

    monkeypatch.setattr(resolution, "get_temporal_client", get_client)
    with pytest.raises(resolution.WorkStopConflict, match="inspect the result"):
        await resolution.stop_clip_work(
            clip_id, "production", ids["production"], expected_status="queued",
            actor="test-operator",
        )
    with db.session_scope() as session:
        assert session.get(Production, ids["production"]).status == "queued"


@pytest.mark.asyncio
async def test_stale_status_and_different_clip_fail_before_temporal(
    fixture_work, monkeypatch,
):
    clip_id, _, _, ids = fixture_work

    async def forbidden():
        raise AssertionError("Temporal must not be touched")

    monkeypatch.setattr(resolution, "get_temporal_client", forbidden)
    with pytest.raises(resolution.WorkStopConflict, match="changed since inspection"):
        await resolution.stop_clip_work(
            clip_id, "production", ids["production"], expected_status="queued",
            actor="test-operator",
        )
    with pytest.raises(LookupError, match="clip not found"):
        await resolution.stop_clip_work(
            uuid.uuid4(), "production", ids["production"], expected_status="review",
            actor="test-operator",
        )


@pytest.mark.asyncio
async def test_ranked_episode_checks_all_stage_executions(fixture_work, monkeypatch):
    clip_id, _, _, ids = fixture_work
    prefix = "purge-episode"
    client = FakeTemporal({
        prefix: WorkflowExecutionStatus.COMPLETED,
        prefix + "-editorial-script": WorkflowExecutionStatus.COMPLETED,
        prefix + "-editorial-voice": WorkflowExecutionStatus.RUNNING,
        prefix + "-editorial-render": WorkflowExecutionStatus.COMPLETED,
    })

    async def get_client():
        return client

    monkeypatch.setattr(resolution, "get_temporal_client", get_client)
    pending = await resolution.stop_clip_work(
        clip_id, "short_episode", ids["short_episode"],
        expected_status="planned", actor="test-operator",
    )
    assert pending.state == "cancellation_requested"
    assert client.handles[prefix + "-editorial-voice"].cancel_count == 1
    with db.session_scope() as session:
        assert session.get(ShortEpisode, ids["short_episode"]).status == "planned"
    client.handles[prefix + "-editorial-voice"].status = WorkflowExecutionStatus.CANCELED
    finished = await resolution.stop_clip_work(
        clip_id, "short_episode", ids["short_episode"],
        expected_status="planned", actor="test-operator",
    )
    assert finished.state == "resolved"
    assert finished.status == "cancelled"


@pytest.mark.asyncio
async def test_unverified_render_attempt_prevents_cancellation(
    fixture_work, monkeypatch,
):
    clip_id, _, _, ids = fixture_work

    def unsafe(*args):
        raise resolution.WorkStopConflict("render attempts are not terminal")

    monkeypatch.setattr(resolution, "_no_media_consumers", unsafe)
    with pytest.raises(resolution.WorkStopConflict, match="render attempts"):
        await resolution.stop_clip_work(
            clip_id, "production", ids["production"], expected_status="review",
            actor="test-operator",
        )
