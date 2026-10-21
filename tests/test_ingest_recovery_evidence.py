"""Read-only held-ingest evidence across Temporal, SourceItem, Clip and R2."""

from __future__ import annotations

import hashlib
import uuid
from types import SimpleNamespace

import pytest
from botocore.exceptions import ClientError
from sqlalchemy import select
from temporalio.client import WorkflowExecutionStatus
from test_command_goals import saved as saved_fixture

from katcha import db
from katcha.domain import ClipStatus, SourceStatus
from katcha.models import Clip, DomainEvent, SourceItem
from katcha.ops import ingest_recovery_evidence as evidence

saved = saved_fixture


def new_source(status=SourceStatus.INGESTING.value, *, metadata=None, clip_id=None):
    source_id = uuid.uuid4()
    with db.session_scope() as session:
        session.add(
            SourceItem(
                id=source_id,
                source_url=f"https://publisher.example.org/{source_id}.mp4?private=token",
                canonical_url=f"https://publisher.example.org/{source_id}.mp4?private=token",
                workflow_id=f"ingest-{source_id}",
                status=status,
                platform="generic",
                clip_id=clip_id,
                source_metadata=metadata or {"rights_lane": "yellow"},
            )
        )
    return source_id


def add_clip(payload: bytes, *, size=None):
    clip_id = uuid.uuid4()
    digest = hashlib.sha256(payload).hexdigest()
    with db.session_scope() as session:
        session.add(
            Clip(
                id=clip_id,
                sha256=digest,
                storage_key=f"raw/{digest[:2]}/{digest}.mp4",
                size_bytes=len(payload) if size is None else size,
                extension="mp4",
                status=ClipStatus.INGESTED.value,
            )
        )
    return clip_id, digest


class Temporal:
    def __init__(self, response):
        self.response = response
        self.lookups = []

    def get_workflow_handle(self, workflow_id):
        self.lookups.append(workflow_id)
        response = self.response

        class Handle:
            async def describe(self):
                if isinstance(response, Exception):
                    raise response
                return SimpleNamespace(status=response)

        return Handle()


class Storage:
    def __init__(self, source_id, *, objects=None, uploads=None, payload=None, head=None):
        self.prefix = f"staging/direct/{source_id}/"
        self.objects = objects if objects is not None else []
        self.uploads = uploads if uploads is not None else []
        self.payload = payload
        self.head = head
        self.settings = SimpleNamespace(s3_bucket="media-fixture")
        self.client = self
        self.calls = []
        self.mutations = 0

    def list_objects_v2(self, **params):
        self.calls.append(("objects", params))
        return {"Contents": list(self.objects), "IsTruncated": False}

    def list_multipart_uploads(self, **params):
        self.calls.append(("uploads", params))
        return {"Uploads": list(self.uploads), "IsTruncated": False}

    def stat(self, key):
        self.calls.append(("head", key))
        if isinstance(self.head, Exception):
            raise self.head
        return self.head or {"size_bytes": len(self.payload or b"")}

    def iter_bytes(self, key, chunk_size):
        self.calls.append(("read", key))
        data = self.payload or b""
        for index in range(0, len(data), chunk_size):
            yield data[index : index + chunk_size]

    def delete_object(self, **_):
        self.mutations += 1
        raise AssertionError("Read-only auditor attempted storage deletion")

    def abort_multipart_upload(self, **_):
        self.mutations += 1
        raise AssertionError("Read-only auditor attempted multipart abort")


@pytest.mark.asyncio
async def test_failed_temporal_with_empty_stage_never_authorizes_replay(saved):
    source_id = new_source()
    client = Temporal(WorkflowExecutionStatus.FAILED)
    store = Storage(source_id)
    before = _snapshot(source_id)
    result = await evidence.inspect_held_ingest(
        source_id, temporal_client=client, store=store
    )
    assert result["assessment"] == "terminal_requires_unlinked_raw_review"
    assert result["unlinked_raw_object_not_excluded"] is True
    assert result["retry_authorized"] is False
    assert result["temporal"] == {
        "visibility": "verified", "status": "FAILED", "terminal": True,
    }
    assert result["direct_staging"]["complete"] is True
    assert result["direct_staging"]["completed_objects"] == 0
    assert client.lookups == [f"ingest-{source_id}"]
    assert _snapshot(source_id) == before
    assert store.mutations == 0


@pytest.mark.asyncio
async def test_ongoing_temporal_execution_is_never_replayed(saved):
    source_id = new_source()
    report = await evidence.inspect_held_ingest(
        source_id,
        temporal_client=Temporal(WorkflowExecutionStatus.RUNNING),
        store=Storage(source_id),
    )
    assert report["assessment"] == "temporal_running_no_replay"
    assert report["temporal"]["terminal"] is False


@pytest.mark.asyncio
async def test_completed_temporal_with_staging_and_incomplete_upload_is_held(saved):
    source_id = new_source()
    stage = f"staging/direct/{source_id}/{'a' * 32}.mp4"
    store = Storage(
        source_id,
        objects=[{"Key": stage}],
        uploads=[{"Key": stage, "UploadId": "opaque-remote-upload-id"}],
    )
    report = await evidence.inspect_held_ingest(
        source_id,
        temporal_client=Temporal(WorkflowExecutionStatus.COMPLETED),
        store=store,
    )
    assert report["assessment"] == "media_or_staging_requires_review"
    assert report["direct_staging"]["completed_objects"] == 1
    assert report["direct_staging"]["incomplete_uploads"] == 1
    assert "opaque-remote-upload-id" not in str(report)
    assert report["retry_authorized"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "client",
    [None, Temporal(ValueError("https://example.org/?token=SECRET"))],
)
async def test_temporal_visibility_unavailable_keeps_raw_secrets_out(saved, client):
    source_id = new_source()
    report = await evidence.inspect_held_ingest(
        source_id, temporal_client=client, store=Storage(source_id)
    )
    assert report["assessment"] == "evidence_incomplete"
    assert report["temporal"]["visibility"] == "unavailable"
    assert "SECRET" not in str(report)
    assert "private=token" not in str(report)


@pytest.mark.asyncio
async def test_recorded_remote_acquisition_not_misclassified_as_direct(saved):
    source_id = new_source(metadata={"remote_acquisition_job_id": str(uuid.uuid4())})
    report = await evidence.inspect_held_ingest(
        source_id,
        temporal_client=Temporal(WorkflowExecutionStatus.FAILED),
        store=Storage(source_id),
    )
    assert report["assessment"] == "remote_acquisition_requires_separate_reconciliation"


@pytest.mark.asyncio
async def test_linked_clip_sha_can_be_verified_with_explicit_opt_in(saved):
    payload = b"complete-media-byte-proof"
    clip_id, _sha = add_clip(payload)
    source_id = new_source(clip_id=clip_id)
    store = Storage(source_id, payload=payload)
    basic = await evidence.inspect_held_ingest(
        source_id, temporal_client=Temporal(WorkflowExecutionStatus.FAILED),
        store=store,
    )
    assert basic["linked_clip_storage"]["state"] == "head_matches_record"
    assert basic["linked_clip_storage"]["sha256_check"] == "not_requested"
    assert not any(name == "read" for name, _ in store.calls)
    verified = await evidence.inspect_held_ingest(
        source_id, temporal_client=Temporal(WorkflowExecutionStatus.FAILED),
        store=store, verify_sha256=True,
    )
    assert verified["linked_clip_id"] == str(clip_id)
    assert verified["linked_clip_storage"]["sha256_check"] == "verified"
    assert verified["assessment"] == "media_or_staging_requires_review"
    assert verified["retry_authorized"] is False


@pytest.mark.asyncio
async def test_linked_clip_byte_mismatch_and_r2_missing_are_reported(saved):
    payload = b"correct media"
    clip_id, _ = add_clip(payload)
    source_id = new_source(clip_id=clip_id)
    mismatch = await evidence.inspect_held_ingest(
        source_id, store=Storage(source_id, payload=b"different"),
        temporal_client=Temporal(WorkflowExecutionStatus.COMPLETED),
        verify_sha256=True,
    )
    assert mismatch["linked_clip_storage"]["state"] == "size_mismatch"
    missing_response = {"Error": {"Code": "NoSuchKey"}}
    missing = await evidence.inspect_held_ingest(
        source_id,
        temporal_client=Temporal(WorkflowExecutionStatus.COMPLETED),
        store=Storage(source_id, head=ClientError(missing_response, "HeadObject")),
    )
    assert missing["linked_clip_storage"]["state"] == "missing"


@pytest.mark.asyncio
async def test_hash_mismatch_with_equal_size_is_detected(saved):
    payload = b"actual-video"
    clip_id, _ = add_clip(payload)
    source_id = new_source(clip_id=clip_id)
    report = await evidence.inspect_held_ingest(
        source_id,
        temporal_client=Temporal(WorkflowExecutionStatus.FAILED),
        store=Storage(source_id, payload=b"invalid-vide"),
        verify_sha256=True,
    )
    assert report["linked_clip_storage"]["sha256_check"] == "mismatch"


@pytest.mark.asyncio
async def test_hash_cap_blocks_unbounded_opt_in_read(saved, monkeypatch):
    payload = b"video content"
    clip_id, _ = add_clip(payload)
    source_id = new_source(clip_id=clip_id)
    monkeypatch.setattr(evidence, "MAX_HASH_BYTES", 4)
    store = Storage(source_id, payload=payload)
    result = await evidence.inspect_held_ingest(
        source_id, temporal_client=Temporal(WorkflowExecutionStatus.FAILED),
        store=store, verify_sha256=True,
    )
    assert result["linked_clip_storage"]["sha256_check"] == "exceeds_verification_limit"
    assert not any(name == "read" for name, _ in store.calls)


@pytest.mark.asyncio
async def test_partial_paginated_inventory_never_means_no_staged_objects(saved):
    source_id = new_source()
    key = f"staging/direct/{source_id}/{'c' * 32}.mp4"

    class Partial(Storage):
        def list_objects_v2(self, **params):
            self.calls.append(("objects", params))
            return {
                "Contents": [{"Key": key}],
                "IsTruncated": True,
                "NextContinuationToken": "more",
            }

    store = Partial(source_id)
    result = await evidence.inspect_held_ingest(
        source_id, temporal_client=Temporal(WorkflowExecutionStatus.FAILED),
        store=store, max_records=1,
    )
    assert result["assessment"] == "evidence_incomplete"
    assert result["direct_staging"]["complete"] is False
    assert result["direct_staging"]["completed_objects"] == 1
    assert len([kind for kind, _ in store.calls if kind == "objects"]) == 1


@pytest.mark.asyncio
async def test_repeated_or_missing_inventory_cursor_fails_closed(saved):
    source_id = new_source()
    stage = f"staging/direct/{source_id}/{'b' * 32}.mp4"

    class Broken(Storage):
        def list_objects_v2(self, **params):
            return {"Contents": [{"Key": stage}], "IsTruncated": True}

    result = await evidence.inspect_held_ingest(
        source_id, temporal_client=Temporal(WorkflowExecutionStatus.FAILED),
        store=Broken(source_id),
    )
    assert result["assessment"] == "evidence_incomplete"
    assert result["direct_staging"]["visibility"] == "unavailable"


@pytest.mark.asyncio
async def test_storage_credentials_and_provider_errors_never_leave_report(saved):
    source_id = new_source()

    class Broken(Storage):
        def list_multipart_uploads(self, **params):
            raise RuntimeError("Authorization: SECRET-R2-KEY signed-url=https://bad.example/")

    result = await evidence.inspect_held_ingest(
        source_id, temporal_client=Temporal(WorkflowExecutionStatus.FAILED),
        store=Broken(source_id),
    )
    assert result["assessment"] == "evidence_incomplete"
    assert "SECRET-R2-KEY" not in str(result)


@pytest.mark.asyncio
async def test_changed_source_after_visibility_probe_blocks_stale_assessment(saved):
    source_id = new_source()
    class Concurrent(Storage):
        def list_multipart_uploads(self, **params):
            with db.session_scope() as session:
                row = session.get(SourceItem, source_id)
                row.status = SourceStatus.READY.value
                row.workflow_id = f"new-{uuid.uuid4()}"
            return super().list_multipart_uploads(**params)

    result = await evidence.inspect_held_ingest(
        source_id,
        temporal_client=Temporal(WorkflowExecutionStatus.FAILED),
        store=Concurrent(source_id),
    )
    assert result["assessment"] == "source_changed_during_inspection"
    assert result["source_changed_during_inspection"] is True


@pytest.mark.asyncio
async def test_non_ingesting_or_missing_source_is_not_recovery_authorized(saved):
    registered = new_source(status=SourceStatus.REGISTERED.value)
    report = await evidence.inspect_held_ingest(
        registered, temporal_client=Temporal(WorkflowExecutionStatus.FAILED),
        store=Storage(registered),
    )
    assert report["assessment"] == "not_an_ingesting_source"
    missing = await evidence.inspect_held_ingest(uuid.uuid4())
    assert missing["assessment"] == "source_not_found"
    assert missing["retry_authorized"] is False


@pytest.mark.asyncio
async def test_argument_validation_is_bounded(saved):
    source_id = new_source()
    with pytest.raises(ValueError, match="max_records"):
        await evidence.inspect_held_ingest(source_id, max_records=0)
    with pytest.raises(ValueError, match="max_records"):
        await evidence.inspect_held_ingest(source_id, max_records=2001)


def _snapshot(source_id):
    with db.session_scope() as session:
        source = session.get(SourceItem, source_id)
        events = list(session.scalars(select(DomainEvent)))
        return source.status, dict(source.source_metadata or {}), len(events)
