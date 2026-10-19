"""Guard ingest restart against previously interrupted Temporal executions.

These are database-backed tests, not proof of a live Temporal/R2 cutover.
"""

from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from temporalio.client import WorkflowExecutionStatus

from test_command_goals import saved as saved_fixture

from katcha import db
from katcha.domain import SourceStatus
from katcha.models import DomainEvent, SourceItem
from katcha.orchestration import ingest_recovery, worker

saved = saved_fixture


def add_source(status=SourceStatus.INGESTING.value):
    source_id = uuid.uuid4()
    with db.session_scope() as session:
        session.add(
            SourceItem(
                id=source_id,
                source_url=f"https://example.org/{source_id}.mp4",
                canonical_url=f"https://example.org/{source_id}.mp4",
                workflow_id=f"ingest-{source_id}",
                platform="generic",
                status=status,
                source_metadata={"channel_profile_id": "channel-1", "rights_lane": "yellow"},
            )
        )
    return source_id


def stored(source_id):
    with db.session_scope() as session:
        source = session.get(SourceItem, source_id)
        assert source is not None
        return {
            "status": source.status,
            "metadata": dict(source.source_metadata or {}),
            "events": list(session.scalars(
                select(DomainEvent).where(DomainEvent.aggregate_id == str(source_id))
            )),
        }


class FakeHandle:
    def __init__(self, response):
        self.response = response

    async def describe(self):
        if isinstance(self.response, Exception):
            raise self.response
        return SimpleNamespace(status=self.response)


class FakeTemporal:
    def __init__(self, response):
        self.response = response
        self.lookups = []
        self.started = []

    def get_workflow_handle(self, workflow_id):
        self.lookups.append(workflow_id)
        return FakeHandle(self.response)

    async def start_workflow(self, workflow, *args, **kwargs):
        self.started.append((workflow, args, kwargs))
        return SimpleNamespace(id=kwargs["id"])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "state",
    [
        WorkflowExecutionStatus.FAILED,
        WorkflowExecutionStatus.COMPLETED,
        WorkflowExecutionStatus.CANCELED,
        WorkflowExecutionStatus.TERMINATED,
        WorkflowExecutionStatus.TIMED_OUT,
    ],
)
async def test_terminal_execution_is_not_replayed_and_has_durable_diagnostic(saved, state):
    source_id = add_source()
    client = FakeTemporal(state)
    with db.session_scope() as session:
        row = session.get(SourceItem, source_id)
        assert await ingest_recovery.check_ingesting_source(client, row) is False
    saved_state = stored(source_id)
    assert saved_state["status"] == "ingesting"
    assert saved_state["metadata"]["rights_lane"] == "yellow"
    detail = saved_state["metadata"][ingest_recovery.RECOVERY_KEY]
    assert detail["code"] == "ingest_temporal_not_running"
    assert detail["workflow_status"] == state.name
    assert detail["retryable_visibility_check"] is False
    assert "observed_at" in detail
    assert len(saved_state["events"]) == 1
    assert saved_state["events"][0].event_type == "source.ingest_recovery_required"
    assert client.started == []

    # The second startup must not grow unbounded audit records or bump timestamps.
    with db.session_scope() as session:
        row = session.get(SourceItem, source_id)
        assert await ingest_recovery.check_ingesting_source(client, row) is False
    next_state = stored(source_id)
    assert next_state["metadata"][ingest_recovery.RECOVERY_KEY] == detail
    assert len(next_state["events"]) == 1


@pytest.mark.asyncio
async def test_running_execution_is_counted_as_present_without_replay(saved):
    source_id = add_source()
    client = FakeTemporal(WorkflowExecutionStatus.RUNNING)
    with db.session_scope() as session:
        row = session.get(SourceItem, source_id)
        assert await ingest_recovery.check_ingesting_source(client, row) is True
    assert ingest_recovery.RECOVERY_KEY not in stored(source_id)["metadata"]
    assert client.started == []


@pytest.mark.asyncio
async def test_running_after_terminal_clears_obsolete_diagnostic_and_audits(saved):
    source_id = add_source()
    client = FakeTemporal(WorkflowExecutionStatus.FAILED)
    with db.session_scope() as session:
        assert await ingest_recovery.check_ingesting_source(
            client, session.get(SourceItem, source_id)
        ) is False
    client.response = WorkflowExecutionStatus.RUNNING
    with db.session_scope() as session:
        assert await ingest_recovery.check_ingesting_source(
            client, session.get(SourceItem, source_id)
        ) is True
    current = stored(source_id)
    assert ingest_recovery.RECOVERY_KEY not in current["metadata"]
    assert [event.event_type for event in current["events"]] == [
        "source.ingest_recovery_required",
        "source.ingest_execution_observed_running",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", [True, False])
async def test_missing_or_unreachable_temporal_never_dispatches_or_exposes_secrets(
    saved, monkeypatch, missing
):
    source_id = add_source()
    class FakeRPC(Exception):
        def __init__(self, status):
            self.status = status
            super().__init__("authorization=PRIVATE secret=NEVER_LOG this is a raw RPC error")
    monkeypatch.setattr(ingest_recovery, "RPCError", FakeRPC)
    monkeypatch.setattr(
        ingest_recovery, "RPCStatusCode", SimpleNamespace(NOT_FOUND="not_found")
    )
    client = FakeTemporal(FakeRPC("not_found" if missing else "unavailable"))
    with db.session_scope() as session:
        assert await ingest_recovery.check_ingesting_source(
            client, session.get(SourceItem, source_id)
        ) is False
    text = str(stored(source_id)["metadata"])
    assert "PRIVATE" not in text
    assert "NEVER_LOG" not in text
    assert (
        "ingest_temporal_execution_missing" if missing
        else "ingest_temporal_visibility_unavailable"
    ) in text
    assert client.started == []


@pytest.mark.asyncio
async def test_temporal_describe_timeout_does_not_trigger_ingest_retry(
    saved, monkeypatch
):
    source_id = add_source()
    monkeypatch.setattr(ingest_recovery, "TEMPORAL_DESCRIBE_TIMEOUT_SECONDS", 0.001)

    class NeverHandle:
        async def describe(self):
            await asyncio.Future()

    class NeverClient(FakeTemporal):
        def get_workflow_handle(self, workflow_id):
            self.lookups.append(workflow_id)
            return NeverHandle()

    client = NeverClient(None)
    with db.session_scope() as session:
        row = session.get(SourceItem, source_id)
    assert await ingest_recovery.check_ingesting_source(client, row) is False
    diagnostic = stored(source_id)["metadata"][ingest_recovery.RECOVERY_KEY]
    assert diagnostic["code"] == "ingest_temporal_visibility_unavailable"
    assert diagnostic["workflow_status"] == "UNVERIFIED"
    assert diagnostic["retryable_visibility_check"] is True
    assert client.started == []


@pytest.mark.asyncio
async def test_concurrent_success_fences_a_stale_startup_diagnostic(saved):
    source_id = add_source()
    client = FakeTemporal(WorkflowExecutionStatus.FAILED)
    row = None
    with db.session_scope() as session:
        row = session.get(SourceItem, source_id)
    with db.session_scope() as session:
        session.get(SourceItem, source_id).status = SourceStatus.READY.value
    assert await ingest_recovery.check_ingesting_source(client, row) is False
    current = stored(source_id)
    assert current["status"] == "ready"
    assert ingest_recovery.RECOVERY_KEY not in current["metadata"]
    assert not current["events"]


@pytest.mark.asyncio
async def test_worker_restarts_registered_but_holds_ingesting_on_failed_execution(
    saved, monkeypatch
):
    registered = add_source(status=SourceStatus.REGISTERED.value)
    ingesting = add_source()
    client = FakeTemporal(WorkflowExecutionStatus.FAILED)
    settings = SimpleNamespace(
        temporal_task_queue="ingest", temporal_publishing_task_queue="publish",
        youtube_processing_poll_seconds=20, youtube_processing_max_polls=10,
        analytics_offsets_hours=lambda: [24, 72],
    )
    resumed, present = await worker._resume_persisted_ingest_and_publication_work(
        client, settings
    )
    assert (resumed, present) == (1, 0)
    assert [args[2]["id"] for args in client.started] == [f"ingest-{registered}"]
    assert client.lookups == [f"ingest-{ingesting}"]
    assert stored(ingesting)["metadata"][ingest_recovery.RECOVERY_KEY][
        "workflow_status"
    ] == "FAILED"


@pytest.mark.asyncio
async def test_worker_running_ingest_is_present_without_duplicate_dispatch(saved):
    source_id = add_source()
    client = FakeTemporal(WorkflowExecutionStatus.RUNNING)
    settings = SimpleNamespace(
        temporal_task_queue="ingest", temporal_publishing_task_queue="publish",
        youtube_processing_poll_seconds=20, youtube_processing_max_polls=10,
        analytics_offsets_hours=lambda: [24, 72],
    )
    assert await worker._resume_persisted_ingest_and_publication_work(
        client, settings
    ) == (0, 1)
    assert client.started == []
    assert stored(source_id)["status"] == "ingesting"
