"""Explicit, evidence-fenced held-ingest completion; never restart a workflow."""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from temporalio.client import WorkflowExecutionStatus
from test_command_goals import saved as saved_fixture
from test_ingest_recovery_evidence import Storage, add_clip, new_source

from katcha import db
from katcha.domain import ClipStatus, SourceStatus
from katcha.models import Clip, DomainEvent, SourceItem
from katcha.ops import linked_ingest_reconciliation as recovery

saved = saved_fixture
RUN_ID = str(uuid.UUID("11111111-2222-4333-8444-555555555555"))


class Temporal:
    def __init__(self, status=WorkflowExecutionStatus.FAILED, run_id=RUN_ID):
        self.status = status
        self.run_id = run_id
        self.calls = []

    def get_workflow_handle(self, workflow_id):
        self.calls.append(workflow_id)
        status, run_id = self.status, self.run_id

        class Handle:
            async def describe(self):
                return SimpleNamespace(status=status, run_id=run_id)

        return Handle()

    async def start_workflow(self, *args, **kwargs):
        raise AssertionError("Recovery must not launch Temporal executions")


def fixture_source(media=b"verified-video-media", *, metadata=None):
    clip_id, digest = add_clip(media)
    source_id = new_source(clip_id=clip_id, metadata=metadata)
    with db.session_scope() as session:
        clip = session.get(Clip, clip_id)
        clip.media_metadata = {
            "streams": [{"codec_type": "video", "width": 1920, "height": 1080}],
            "format": {"duration": "12.5"},
        }
    return source_id, clip_id, digest, Storage(source_id, payload=media)


def state(source_id):
    with db.session_scope() as session:
        item = session.get(SourceItem, source_id)
        events = list(session.scalars(
            select(DomainEvent).where(DomainEvent.aggregate_id == str(source_id))
        ))
        return {
            "status": item.status,
            "error": item.error,
            "metadata": dict(item.source_metadata or {}),
            "clip_id": item.clip_id,
            "events": events,
        }


@pytest.mark.asyncio
async def test_plan_requires_real_bytes_and_never_modifies_source(saved):
    source_id, clip_id, sha, store = fixture_source()
    prior = state(source_id)
    temporal = Temporal()
    plan = await recovery.plan_linked_clip_recovery(
        source_id, temporal_client=temporal, store=store,
    )
    assert plan["eligible"] is True
    assert plan["approval_fingerprint"] and len(plan["approval_fingerprint"]) == 64
    assert plan["proposed_action"] == "mark_existing_verified_clip_ready"
    assert plan["retry_authorized"] is False
    assert plan["temporal"]["run_id"] == RUN_ID
    assert plan["clip_sha256"] == sha
    assert plan["linked_clip_id"] == str(clip_id)
    assert temporal.calls == [f"ingest-{source_id}"]
    assert state(source_id) == prior
    assert store.mutations == 0
    assert any(name == "read" for name, _ in store.calls)


@pytest.mark.asyncio
async def test_explicit_approval_marks_existing_clip_ready_without_storage_mutation(saved):
    source_id, clip_id, digest, store = fixture_source(
        metadata={"rights_lane": "yellow", "channel_profile_id": "channel-a",
                  "ingest_recovery": {"code": "ingest_temporal_not_running"},
                  "ingestion_diagnostic": {"code": "transient"}},
    )
    temporal = Temporal()
    preview = await recovery.plan_linked_clip_recovery(
        source_id, temporal_client=temporal, store=store,
    )
    result = await recovery.finalize_linked_clip_recovery(
        source_id,
        approved_fingerprint=preview["approval_fingerprint"],
        temporal_client=temporal, store=store,
    )
    assert result == {
        "source_id": str(source_id), "clip_id": str(clip_id),
        "status": "ready", "reconciled": True,
        "workflow_started": False, "storage_mutated": False,
    }
    committed = state(source_id)
    assert committed["clip_id"] == clip_id
    assert committed["status"] == SourceStatus.READY.value
    assert committed["metadata"]["rights_lane"] == "yellow"
    assert committed["metadata"]["channel_profile_id"] == "channel-a"
    assert "ingest_recovery" not in committed["metadata"]
    assert "ingestion_diagnostic" not in committed["metadata"]
    proof = committed["metadata"]["manual_ingest_reconciliation"]
    assert proof["clip_sha256"] == digest
    assert proof["workflow_run_id"] == RUN_ID
    assert proof["evidence_fingerprint"] == preview["approval_fingerprint"]
    assert [event.event_type for event in committed["events"]] == [
        "source.ingest_reconciled_linked_clip"
    ]
    assert store.mutations == 0
    with pytest.raises(recovery.RecoveryApprovalError):
        await recovery.finalize_linked_clip_recovery(
            source_id,
            approved_fingerprint=preview["approval_fingerprint"],
            temporal_client=temporal, store=store,
        )
    assert len(state(source_id)["events"]) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    ["wrong_fingerprint", "run_changed", "workflow_running", "clip_status_failed",
     "metadata_changed", "stage_added", "raw_bytes_corrupted"],
)
async def test_changed_or_unverifiable_evidence_blocks_transaction(saved, change):
    source_id, clip_id, _, store = fixture_source()
    temporal = Temporal()
    plan = await recovery.plan_linked_clip_recovery(
        source_id, temporal_client=temporal, store=store,
    )
    assert plan["eligible"] is True
    approved = plan["approval_fingerprint"]
    if change == "wrong_fingerprint":
        approved = "0" * 64
    elif change == "run_changed":
        temporal.run_id = str(uuid.uuid4())
    elif change == "workflow_running":
        temporal.status = WorkflowExecutionStatus.RUNNING
    elif change == "clip_status_failed":
        with db.session_scope() as session:
            session.get(Clip, clip_id).status = ClipStatus.FAILED.value
    elif change == "metadata_changed":
        with db.session_scope() as session:
            item = session.get(SourceItem, source_id)
            item.source_metadata = {**item.source_metadata, "operator_note": "changed"}
    elif change == "stage_added":
        store.objects = [{
            "Key": f"staging/direct/{source_id}/{'f'*32}.mp4"
        }]
    elif change == "raw_bytes_corrupted":
        store.payload = b"x" * len(store.payload)
    with pytest.raises(recovery.RecoveryApprovalError):
        await recovery.finalize_linked_clip_recovery(
            source_id, approved_fingerprint=approved,
            temporal_client=temporal, store=store,
        )
    assert state(source_id)["status"] == "ingesting"
    assert not state(source_id)["events"]
    assert store.mutations == 0


@pytest.mark.asyncio
async def test_failed_source_with_linked_verified_clip_can_reconcile(saved):
    source_id, clip_id, _, store = fixture_source()
    with db.session_scope() as session:
        row = session.get(SourceItem, source_id)
        row.status = SourceStatus.FAILED.value
        row.error = "Prior ingest workflow failed after persisting Clip"
    temporal = Temporal(status=WorkflowExecutionStatus.FAILED)
    plan = await recovery.plan_linked_clip_recovery(
        source_id, temporal_client=temporal, store=store,
    )
    assert plan["eligible"] is True
    done = await recovery.finalize_linked_clip_recovery(
        source_id, approved_fingerprint=plan["approval_fingerprint"],
        temporal_client=temporal, store=store,
    )
    assert done["clip_id"] == str(clip_id)
    assert done["status"] == SourceStatus.READY.value
    assert state(source_id)["error"] is None
    assert store.mutations == 0


@pytest.mark.asyncio
async def test_post_verification_db_race_is_fenced_under_row_lock(saved, monkeypatch):
    source_id, _, _, store = fixture_source()
    temporal = Temporal()
    plan = await recovery.plan_linked_clip_recovery(
        source_id, temporal_client=temporal, store=store,
    )
    original = recovery.plan_linked_clip_recovery

    async def changed_after_plan(*args, **kwargs):
        approved = await original(*args, **kwargs)
        with db.session_scope() as session:
            session.get(SourceItem, source_id).workflow_id = f"other-{uuid.uuid4()}"
        return approved

    monkeypatch.setattr(recovery, "plan_linked_clip_recovery", changed_after_plan)
    with pytest.raises(recovery.RecoveryApprovalError, match="changed before recovery commit"):
        await recovery.finalize_linked_clip_recovery(
            source_id, approved_fingerprint=plan["approval_fingerprint"],
            temporal_client=temporal, store=store,
        )
    assert state(source_id)["status"] == "ingesting"
    assert not state(source_id)["events"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case,blocker",
    [
        ("no_clip", "linked_clip_missing"),
        ("no_video", "clip_video_metadata_missing"),
        ("unknown_run", "temporal_terminal_run_unverified"),
        ("remote_job", "remote_acquisition_separate"),
        ("registered", "source_not_recoverable"),
        ("wrong_key", "clip_object_key_not_content_addressed"),
    ],
)
async def test_preview_refuses_unreconciled_or_unowned_media(saved, case, blocker):
    source_id, clip_id, _, store = fixture_source()
    temporal = Temporal()
    with db.session_scope() as session:
        item = session.get(SourceItem, source_id)
        clip = session.get(Clip, clip_id)
        if case == "no_clip":
            item.clip_id = None
        elif case == "no_video":
            clip.media_metadata = {"streams": [{"codec_type": "audio"}]}
        elif case == "remote_job":
            item.source_metadata = {"remote_acquisition_job_id": str(uuid.uuid4())}
        elif case == "registered":
            item.status = SourceStatus.REGISTERED.value
        elif case == "wrong_key":
            clip.storage_key = "raw/unauthorized.mp4"
    if case == "unknown_run":
        temporal.run_id = None
    plan = await recovery.plan_linked_clip_recovery(
        source_id, temporal_client=temporal, store=store,
    )
    assert plan["eligible"] is False
    assert blocker in plan["blockers"]
    assert plan["approval_fingerprint"] is None
    assert state(source_id)["events"] == []


@pytest.mark.asyncio
async def test_untrusted_workflow_identity_cannot_be_approved_or_exposed(saved):
    source_id, _, _, store = fixture_source()
    with db.session_scope() as session:
        session.get(SourceItem, source_id).workflow_id = (
            "https://private.example/run?token=DO_NOT_LOG"
        )
    plan = await recovery.plan_linked_clip_recovery(
        source_id, temporal_client=Temporal(), store=store,
    )
    assert plan["eligible"] is False
    assert "workflow_identity_untrusted" in plan["blockers"]
    assert plan["source_snapshot"] is None
    assert plan["workflow_id"] == "<redacted>"
    assert "DO_NOT_LOG" not in str(plan)


@pytest.mark.asyncio
async def test_invalid_approval_token_never_queries_remote_services(saved):
    source_id, _, _, store = fixture_source()
    temporal = Temporal()
    with pytest.raises(recovery.RecoveryApprovalError):
        await recovery.finalize_linked_clip_recovery(
            source_id, approved_fingerprint="approval",
            temporal_client=temporal, store=store,
        )
    assert temporal.calls == []
    assert not store.calls


@pytest.mark.asyncio
async def test_missing_source_and_missing_temporal_remain_read_only(saved):
    plan = await recovery.plan_linked_clip_recovery(uuid.uuid4())
    assert plan["eligible"] is False
    assert "source_missing" in plan["blockers"]
    source_id, _, _, store = fixture_source()
    plan = await recovery.plan_linked_clip_recovery(source_id, store=store)
    assert "temporal_terminal_run_unverified" in plan["blockers"]
    assert not state(source_id)["events"]
