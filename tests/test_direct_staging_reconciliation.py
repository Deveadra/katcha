"""Reconcile only aged, unreferenced, terminal direct acquisition staging."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from test_command_goals import saved as saved_fixture

from katcha import db
from katcha.domain import SourceStatus
from katcha.models import Clip, SourceItem
from katcha.ops import direct_staging_reconciliation as recon

saved = saved_fixture
NOW = datetime(2026, 10, 19, 12, 0, tzinfo=UTC)


def key(source_id, attempt="0" * 32, extension="mp4"):
    return f"staging/direct/{source_id}/{attempt}.{extension}"


def source(status):
    source_id = uuid.uuid4()
    with db.session_scope() as session:
        session.add(SourceItem(
            id=source_id, source_url=f"https://example.org/{source_id}.mp4",
            canonical_url=f"https://example.org/{source_id}.mp4",
            platform="generic", status=status,
        ))
    return source_id


class FakeClient:
    def __init__(self, object_pages=None, upload_pages=None):
        self.object_pages = object_pages or [{"Contents": [], "IsTruncated": False}]
        self.upload_pages = upload_pages or [{"Uploads": [], "IsTruncated": False}]
        self.object_calls = []
        self.upload_calls = []
        self.aborted = []

    def list_objects_v2(self, **kwargs):
        self.object_calls.append(kwargs)
        return self.object_pages[len(self.object_calls) - 1]

    def list_multipart_uploads(self, **kwargs):
        self.upload_calls.append(kwargs)
        return self.upload_pages[len(self.upload_calls) - 1]

    def abort_multipart_upload(self, **kwargs):
        self.aborted.append(kwargs)


class FakeStore:
    def __init__(self, objects=None, uploads=None):
        self.client = FakeClient(objects, uploads)
        self.settings = SimpleNamespace(s3_bucket="katcha-media")
        self.deleted = []

    def delete(self, key):
        self.deleted.append(key)


def obj(path, *, hours=50):
    return {"Key": path, "LastModified": NOW - timedelta(hours=hours)}


def upload(path, *, hours=50, upload_id="upload-1"):
    return {
        "Key": path,
        "Initiated": NOW - timedelta(hours=hours),
        "UploadId": upload_id,
    }


@pytest.fixture(autouse=True)
def fake_mutation_authority(monkeypatch):
    monkeypatch.setattr(recon, "assert_mutation_authority", lambda *a, **kw: None)


def test_dry_run_reports_both_staging_types_and_does_not_mutate(saved):
    ready = source(SourceStatus.READY.value)
    failed = source(SourceStatus.FAILED.value)
    store = FakeStore(
        [{"Contents": [obj(key(ready))], "IsTruncated": False}],
        [{"Uploads": [upload(key(failed))], "IsTruncated": False}],
    )
    summary = recon.reconcile_direct_staging(store=store, now=NOW)
    assert summary["mode"] == "dry_run"
    assert summary["scope"] == "staging/direct/"
    assert summary["counts"] == {"scanned": 2, "eligible": 2}
    assert summary["remote_acquisition_staging_excluded"] is True
    assert len(summary["candidate_examples"]) == 2
    assert not store.deleted
    assert not store.client.aborted


def test_apply_deletes_old_terminal_source_and_aborts_its_incomplete_upload(saved):
    ready = source(SourceStatus.READY.value)
    failed = source(SourceStatus.FAILED.value)
    ready_key = key(ready)
    failed_key = key(failed)
    store = FakeStore(
        [{"Contents": [obj(ready_key)], "IsTruncated": False}],
        [{"Uploads": [upload(failed_key, upload_id="only-abort-this")], "IsTruncated": False}],
    )
    summary = recon.reconcile_direct_staging(store=store, now=NOW, apply=True)
    assert summary["counts"]["removed"] == 2
    assert store.deleted == [ready_key]
    assert store.client.aborted == [{
        "Bucket": "katcha-media",
        "Key": failed_key,
        "UploadId": "only-abort-this",
    }]


def test_active_unknown_recent_or_malformed_staging_is_never_modified(saved):
    active = source(SourceStatus.INGESTING.value)
    registered = source(SourceStatus.REGISTERED.value)
    ready = source(SourceStatus.READY.value)
    examples = [
        obj(key(active)), obj(key(registered)),
        obj(key(uuid.uuid4())), obj(key(ready), hours=1),
        obj("acquisition-staging/channel/job/1/media"),
        obj("staging/direct/../../raw/clip.mp4"),
        obj(key(ready, extension="txt")),
    ]
    store = FakeStore(
        [{"Contents": examples, "IsTruncated": False}],
        [{"Uploads": [upload(key(active), upload_id="in-flight")], "IsTruncated": False}],
    )
    summary = recon.reconcile_direct_staging(store=store, now=NOW, apply=True)
    assert summary["counts"].get("removed", 0) == 0
    assert summary["counts"]["source_not_terminal"] == 3
    assert summary["counts"]["unknown_source"] == 1
    assert summary["counts"]["recent_or_undated"] == 1
    assert summary["counts"]["unrecognized_key"] == 2
    assert summary["counts"]["out_of_scope"] == 1
    assert not store.deleted and not store.client.aborted


def test_existing_clip_storage_reference_blocks_removal(saved):
    ready = source(SourceStatus.READY.value)
    staged = key(ready)
    with db.session_scope() as session:
        session.add(Clip(
            id=uuid.uuid4(), sha256="a" * 64, storage_key=staged,
        ))
    store = FakeStore(
        [{"Contents": [obj(staged)], "IsTruncated": False}]
    )
    result = recon.reconcile_direct_staging(store=store, now=NOW, apply=True)
    assert result["counts"]["referenced_by_clip"] == 1
    assert store.deleted == []


def test_recheck_before_destructive_action_observes_changed_source(saved, monkeypatch):
    ready = source(SourceStatus.READY.value)
    staged = key(ready)
    store = FakeStore(
        [{"Contents": [obj(staged)], "IsTruncated": False}]
    )
    original = recon._eligible_source
    def changed_after_snapshot(path, *, lock):
        valid, reason = original(path, lock=lock)
        with db.session_scope() as session:
            row = session.get(SourceItem, ready)
            row.status = SourceStatus.INGESTING.value
        return valid, reason

    monkeypatch.setattr(recon, "_eligible_source", changed_after_snapshot)
    result = recon.reconcile_direct_staging(store=store, now=NOW, apply=True)
    assert result["counts"]["eligible"] == 1
    assert result["counts"]["source_not_terminal"] == 1
    assert store.deleted == []


def test_scans_multiple_pages_with_explicit_nonrepeating_markers(saved):
    ready = source(SourceStatus.READY.value)
    one = key(ready, "a" * 32)
    two = key(ready, "b" * 32)
    pages = [
        {"Contents": [obj(one)], "IsTruncated": True, "NextContinuationToken": "page-2"},
        {"Contents": [obj(two)], "IsTruncated": False},
    ]
    upages = [
        {
            "Uploads": [upload(one, upload_id="first")],
            "IsTruncated": True, "NextKeyMarker": one,
            "NextUploadIdMarker": "first",
        },
        {"Uploads": [upload(two, upload_id="second")], "IsTruncated": False},
    ]
    store = FakeStore(pages, upages)
    result = recon.reconcile_direct_staging(store=store, now=NOW)
    assert result["counts"]["eligible"] == 4
    assert store.client.object_calls[1]["ContinuationToken"] == "page-2"
    assert store.client.upload_calls[1]["KeyMarker"] == one
    assert store.client.upload_calls[1]["UploadIdMarker"] == "first"


def test_bounded_scan_never_walks_next_page(saved):
    ready = source(SourceStatus.READY.value)
    stage = key(ready)
    store = FakeStore([
        {"Contents": [obj(stage)], "IsTruncated": True, "NextContinuationToken": "later"},
    ])
    result = recon.reconcile_direct_staging(store=store, now=NOW, max_records=1)
    assert result["counts"]["scanned"] == 1
    assert len(store.client.object_calls) == 1


@pytest.mark.parametrize("hours,max_records", [(0, 200), (23, 200), (24, 0), (24, 2001)])
def test_rejects_nonconservative_execution_controls(hours, max_records):
    with pytest.raises(ValueError):
        recon.reconcile_direct_staging(
            min_age_hours=hours, max_records=max_records,
            store=FakeStore(), now=NOW, apply=True,
        )


def test_invalid_cursor_fails_closed(saved):
    ready = source(SourceStatus.READY.value)
    store = FakeStore([
        {"Contents": [obj(key(ready))], "IsTruncated": True},
    ])
    with pytest.raises(RuntimeError, match="pagination cursor"):
        recon.reconcile_direct_staging(store=store, now=NOW)


def test_apply_requires_both_inventories_complete_before_first_delete(saved):
    ready = source(SourceStatus.READY.value)
    candidate = key(ready)
    store = FakeStore(
        [{"Contents": [obj(candidate)], "IsTruncated": False}],
        [{
            "Uploads": [upload(candidate)],
            "IsTruncated": True,
            # A broken multipart cursor must fail before the object mutation.
        }],
    )
    with pytest.raises(RuntimeError, match="pagination cursor"):
        recon.reconcile_direct_staging(store=store, now=NOW, apply=True)
    assert not store.deleted
    assert not store.client.aborted


def test_empty_paginated_inventory_is_bounded(saved):
    store = FakeStore([
        {"Contents": [], "IsTruncated": True, "NextContinuationToken": "p2"},
    ])
    with pytest.raises(RuntimeError, match="page bound"):
        recon.reconcile_direct_staging(store=store, now=NOW, max_records=1)
    assert len(store.client.object_calls) == 1


def test_invalid_undated_and_boundary_objects_are_protected(saved):
    ready = source(SourceStatus.READY.value)
    staged = key(ready)
    items = [
        {"Key": staged, "LastModified": NOW - timedelta(hours=24)},
        {"Key": key(ready, "b" * 32), "LastModified": None},
        {"Key": key(ready, "c" * 32), "LastModified": NOW.replace(tzinfo=None)},
    ]
    store = FakeStore([{"Contents": items, "IsTruncated": False}])
    result = recon.reconcile_direct_staging(store=store, now=NOW, apply=True)
    assert result["counts"]["recent_or_undated"] == 3
    assert not store.deleted


def test_absent_upload_id_never_aborts(saved):
    ready = source(SourceStatus.READY.value)
    store = FakeStore(
        uploads=[{"Uploads": [upload(key(ready), upload_id="")], "IsTruncated": False}]
    )
    result = recon.reconcile_direct_staging(store=store, now=NOW, apply=True)
    assert result["counts"]["missing_upload_id"] == 1
    assert not store.client.aborted


def test_uuid_stage_prefix_validation_excludes_remote_jobs_and_extra_paths():
    source_id = uuid.uuid4()
    assert recon.stage_source_id(key(source_id)) == source_id
    assert recon.stage_source_id(f"acquisition-staging/{source_id}/media") is None
    assert recon.stage_source_id(key(source_id) + "/other") is None
    assert recon.stage_source_id(f"staging/direct/{source_id}/../media.mp4") is None
    assert recon.stage_source_id(key(source_id, extension="mp4.exe")) is None
