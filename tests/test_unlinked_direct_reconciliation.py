"""Source-owned raw-object reconciliation, never SHA-only attribution."""

from __future__ import annotations

import hashlib
import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from temporalio.client import WorkflowExecutionStatus
from test_command_goals import saved as saved_fixture
from test_ingest_recovery_evidence import Storage, new_source
from test_linked_ingest_reconciliation import Temporal

from katcha import db
from katcha.domain import SourceStatus
from katcha.models import Clip, DomainEvent, SourceItem
from katcha.ops import direct_promotion_provenance as provenance
from katcha.ops import unlinked_direct_reconciliation as recovery
from katcha.ops.linked_ingest_reconciliation import RecoveryApprovalError

saved = saved_fixture
VIDEO = b"verified-publisher-media" * 13
MEDIA = {
    "format": {"duration": "12.5", "size": str(len(VIDEO))},
    "streams": [{"codec_type": "video", "codec_name": "mpeg4", "width": 1920, "height": 1080}],
}


class R2(Storage):
    def __init__(self, source_id, payload=VIDEO):
        super().__init__(source_id, payload=payload)
        self.objects = []
        self.uploads = []


def stage(source_id, extension="mp4"):
    return f"staging/direct/{source_id}/{'f' * 32}.{extension}"


def source_with_verified_claim(monkeypatch, *, payload=VIDEO):
    source_id = new_source()
    with db.session_scope() as session:
        source = session.get(SourceItem, source_id)
        url = source.source_url
    sha = hashlib.sha256(payload).hexdigest()
    monkeypatch.setattr(
        recovery, "inspect_stored_media",
        lambda _store, _key, _size: MEDIA,
    )
    claim = provenance.record_verified_direct_promotion(
        source_id,
        canonical_url=url,
        stage_key=stage(source_id),
        sha256=sha,
        size_bytes=len(payload),
        extension="mp4",
        media_metadata=MEDIA,
    )
    return source_id, R2(source_id, payload), claim


def db_state(source_id):
    with db.session_scope() as session:
        source = session.get(SourceItem, source_id)
        clips = list(session.scalars(select(Clip)))
        events = list(session.scalars(
            select(DomainEvent).where(DomainEvent.aggregate_id == str(source_id))
        ))
        return {
            "status": source.status,
            "clip_id": source.clip_id,
            "metadata": dict(source.source_metadata or {}),
            "clips": clips,
            "events": events,
        }


@pytest.mark.asyncio
async def test_pre_promotion_claim_and_audit_link_source_to_exact_bytes(saved, monkeypatch):
    source_id, store, claim = source_with_verified_claim(monkeypatch)
    assert claim["source_id"] == str(source_id)
    assert claim["sha256"] == hashlib.sha256(VIDEO).hexdigest()
    assert "private=token" not in str(claim)
    assert provenance.validate_proof(claim, source_id)
    assert provenance.proof_fingerprint(claim) == (
        db_state(source_id)["events"][0].payload["claim_sha256"]
    )
    before = db_state(source_id)
    again = provenance.record_verified_direct_promotion(
        source_id,
        canonical_url=before["metadata"][provenance.PROOF_KEY]
        and "https://publisher.example.org/" + str(source_id) + ".mp4?private=token",
        stage_key=stage(source_id),
        sha256=claim["sha256"],
        size_bytes=len(VIDEO),
        extension="mp4",
        media_metadata=MEDIA,
    )
    assert again == claim
    assert len(db_state(source_id)["events"]) == 1
    assert not store.calls


@pytest.mark.asyncio
async def test_supervised_recovery_reconstructs_original_clip_without_r2_changes(
    saved, monkeypatch
):
    source_id, store, claim = source_with_verified_claim(monkeypatch)
    temporal = Temporal()
    before = db_state(source_id)
    plan = await recovery.plan_unlinked_direct_recovery(
        source_id, temporal_client=temporal, store=store,
    )
    assert plan["eligible"]
    assert plan["approval_fingerprint"]
    assert plan["retry_authorized"] is False
    assert plan["proof_sha256"] == claim["sha256"]
    assert db_state(source_id)["status"] == before["status"]
    assert db_state(source_id)["clip_id"] is None
    assert len(before["clips"]) == 0

    result = await recovery.finalize_unlinked_direct_recovery(
        source_id,
        approved_fingerprint=plan["approval_fingerprint"],
        temporal_client=temporal, store=store,
    )
    assert result["status"] == "ready"
    assert result["reconciled"] is True
    assert result["workflow_started"] is False and result["storage_mutated"] is False
    updated = db_state(source_id)
    assert updated["clip_id"] == uuid.UUID(result["clip_id"])
    assert len(updated["clips"]) == 1
    clip = updated["clips"][0]
    assert clip.sha256 == claim["sha256"]
    assert clip.storage_key == claim["raw_key"]
    assert clip.size_bytes == len(VIDEO)
    assert (clip.width, clip.height) == (1920, 1080)
    assert updated["metadata"]["rights_lane"] == "yellow"
    assert updated["metadata"]["manual_ingest_reconciliation"]["claim_sha256"] == (
        provenance.proof_fingerprint(claim)
    )
    assert [e.event_type for e in updated["events"]] == [
        "source.direct_media_stage_verified",
        "source.ingest_reconciled_verified_unlinked_raw",
    ]
    assert store.mutations == 0
    with pytest.raises(RecoveryApprovalError):
        await recovery.finalize_unlinked_direct_recovery(
            source_id,
            approved_fingerprint=plan["approval_fingerprint"],
            temporal_client=temporal, store=store,
        )
    assert len(db_state(source_id)["events"]) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure,reason",
    [
        ("unproven", "durable_provenance_missing"),
        ("no_event", "verified_promotion_event_missing"),
        ("other_run", "terminal_temporal_run_unverified"),
        ("stage", "staging_not_reconciled"),
        ("mutated_url", "canonical_source_changed"),
        ("remote", "leased_remote_acquisition_excluded"),
        ("bytes", "raw_object_not_verified"),
        ("other_clip", "existing_clip_collision_requires_review"),
        ("already_ready", "source_not_ingesting"),
    ],
)
async def test_unknown_or_conflicting_media_never_approves(saved, monkeypatch, failure, reason):
    source_id, store, claim = source_with_verified_claim(monkeypatch)
    temporal = Temporal()
    with db.session_scope() as session:
        source = session.get(SourceItem, source_id)
        if failure == "unproven":
            source.source_metadata = {"rights_lane": "yellow"}
        elif failure == "no_event":
            session.query(DomainEvent).delete()
        elif failure == "mutated_url":
            source.source_url = f"https://new.example/{source_id}.mp4"
        elif failure == "remote":
            source.source_metadata = {
                **source.source_metadata, "remote_acquisition_job_id": str(uuid.uuid4())
            }
        elif failure == "other_clip":
            session.add(Clip(sha256=claim["sha256"], storage_key=claim["raw_key"]))
        elif failure == "already_ready":
            source.status = SourceStatus.READY.value
    if failure == "other_run":
        temporal.status = WorkflowExecutionStatus.RUNNING
    elif failure == "stage":
        store.objects = [{"Key": stage(source_id)}]
    elif failure == "bytes":
        store.payload = b"0" * len(VIDEO)
    plan = await recovery.plan_unlinked_direct_recovery(
        source_id, temporal_client=temporal, store=store,
    )
    assert reason in plan["blockers"]
    assert plan["eligible"] is False
    assert plan["approval_fingerprint"] is None


@pytest.mark.asyncio
async def test_stale_approval_rechecks_source_and_detects_no_ownership(saved, monkeypatch):
    source_id, store, _ = source_with_verified_claim(monkeypatch)
    temporal = Temporal()
    plan = await recovery.plan_unlinked_direct_recovery(
        source_id, temporal_client=temporal, store=store,
    )
    assert plan["eligible"]
    with db.session_scope() as session:
        source = session.get(SourceItem, source_id)
        source.source_metadata = {**source.source_metadata, "operator_note": "changed"}
    with pytest.raises(RecoveryApprovalError):
        await recovery.finalize_unlinked_direct_recovery(
            source_id,
            approved_fingerprint=plan["approval_fingerprint"],
            temporal_client=temporal, store=store,
        )
    assert db_state(source_id)["clip_id"] is None


@pytest.mark.asyncio
async def test_db_race_between_verify_and_commit_is_fenced(saved, monkeypatch):
    source_id, store, _ = source_with_verified_claim(monkeypatch)
    temporal = Temporal()
    plan = await recovery.plan_unlinked_direct_recovery(
        source_id, temporal_client=temporal, store=store,
    )
    original = recovery.plan_unlinked_direct_recovery

    async def concurrent_update(*args, **kwargs):
        fresh = await original(*args, **kwargs)
        with db.session_scope() as session:
            source = session.get(SourceItem, source_id)
            source.canonical_url = "https://publisher.example.org/other.mp4"
            source.source_url = "https://publisher.example.org/other.mp4"
        return fresh

    monkeypatch.setattr(recovery, "plan_unlinked_direct_recovery", concurrent_update)
    with pytest.raises(RecoveryApprovalError, match="changed before commit"):
        await recovery.finalize_unlinked_direct_recovery(
            source_id,
            approved_fingerprint=plan["approval_fingerprint"],
            temporal_client=temporal, store=store,
        )
    assert db_state(source_id)["clip_id"] is None


def test_unresolved_previous_stage_claim_cannot_be_overwritten(saved, monkeypatch):
    source_id, _, claim = source_with_verified_claim(monkeypatch)
    with pytest.raises(RuntimeError, match="earlier verified direct-media attempt"):
        provenance.record_verified_direct_promotion(
            source_id,
            canonical_url="https://publisher.example.org/" + str(source_id) + ".mp4?private=token",
            stage_key=stage(source_id).replace("f" * 32, "e" * 32),
            sha256=claim["sha256"], size_bytes=len(VIDEO),
            extension="mp4", media_metadata=MEDIA,
        )
    assert len(db_state(source_id)["events"]) == 1


@pytest.mark.parametrize(
    "mutate",
    [
        lambda p: p.update(stage_key="staging/direct/../../tmp.mp4"),
        lambda p: p.update(source_id=str(uuid.uuid4())),
        lambda p: p.update(sha256="0" * 63),
        lambda p: p.update(size_bytes=True),
        lambda p: p.update(extension=["mp4"]),
        lambda p: p.update(media_metadata={"streams": [{"codec_type": "audio"}]}),
    ],
)
def test_malformed_provenance_fails_closed(saved, monkeypatch, mutate):
    source_id, _, claim = source_with_verified_claim(monkeypatch)
    modified = dict(claim)
    mutate(modified)
    assert not provenance.validate_proof(modified, source_id)


@pytest.mark.asyncio
async def test_invalid_approval_string_does_not_read_r2_or_temporal(saved, monkeypatch):
    source_id, store, _ = source_with_verified_claim(monkeypatch)
    temporal = Temporal()
    with pytest.raises(RecoveryApprovalError):
        await recovery.finalize_unlinked_direct_recovery(
            source_id, approved_fingerprint="bad", temporal_client=temporal, store=store
        )
    assert temporal.calls == []
    assert store.calls == []
