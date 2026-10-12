"""Measured owned-footage fixtures; no provider calls or inferred semantic events."""

from __future__ import annotations

import hashlib
import subprocess
import uuid
from datetime import UTC, datetime, timedelta
from fractions import Fraction

import pytest
from PIL import Image
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from katcha import db
from katcha.editorial.media_intake import scene_index, scene_worker
from katcha.editorial.media_intake.scene_schemas import SearchShotsRequest
from katcha.editorial.project_schemas import CreateEditorialProject, SaveEditorialDraft
from katcha.editorial_models import ProjectMediaItem
from katcha.intelligence_models import ChannelProfile
from katcha.models import Clip, SourceItem
from katcha.scene_index_models import MediaSceneIndex
from katcha.services.editorial_media_bin import add_project_media_item
from katcha.services.editorial_projects import create_project, save_draft


class MemoryStore:
    def __init__(self, data):
        self.data = data
        self.calls = 0
        self.after_read = None

    def iter_bytes(self, key, chunk_size=1024 * 1024):
        assert key == "canonical/owned.mkv"
        self.calls += 1
        yield self.data
        if self.after_read:
            self.after_read()


@pytest.fixture
def known_video(tmp_path):
    av = pytest.importorskip("av")
    path = tmp_path / "known-three-shots.mkv"
    with av.open(str(path), "w") as container:
        stream = container.add_stream("ffv1", rate=10)
        stream.width, stream.height, stream.pix_fmt = 64, 36, "yuv444p"
        for number in range(30):
            color = ("red", "lime", "blue")[number // 10]
            frame = av.VideoFrame.from_image(Image.new("RGB", (64, 36), color))
            frame.pts = number + 50  # Preserve and normalize a nonzero source origin.
            frame.time_base = Fraction(1, 10)
            for packet in stream.encode(frame):
                container.mux(packet)
        for packet in stream.encode():
            container.mux(packet)
    return path


@pytest.fixture
def indexed_context(monkeypatch, tmp_path, known_video):
    db.load_model_metadata()
    engine = create_engine(f"sqlite:///{tmp_path / 'index.sqlite'}")
    db.Base.metadata.create_all(engine)
    monkeypatch.setattr(db, "SessionLocal", sessionmaker(bind=engine, expire_on_commit=False))
    channel, other = uuid.uuid4(), uuid.uuid4()
    data = known_video.read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    clip_id, source_id = uuid.uuid4(), uuid.uuid4()
    with db.session_scope() as session:
        session.add_all(
            [
                ChannelProfile(
                    id=value,
                    youtube_connection_id=uuid.uuid4(),
                    status="active",
                    timezone="UTC",
                    profile_metadata={},
                )
                for value in (channel, other)
            ]
        )
        session.add(
            Clip(
                id=clip_id,
                sha256=digest,
                storage_key="canonical/owned.mkv",
                size_bytes=len(data),
                extension="mkv",
                status="ingested",
                media_metadata={},
            )
        )
        session.add(
            SourceItem(
                id=source_id,
                source_url=f"https://upload.katcha.invalid/{source_id}",
                canonical_url=f"https://upload.katcha.invalid/{source_id}",
                platform="upload",
                clip_id=clip_id,
                status="ready",
                source_metadata={
                    "channel_profile_id": str(channel),
                    "permitted_use_confirmed": True,
                },
            )
        )
    project = create_project(
        channel,
        CreateEditorialProject.model_validate(
            {
                "idempotency_key": "shots-project",
                "brief": {
                    "prompt": "Measured footage",
                    "source_urls": [f"https://upload.katcha.invalid/{source_id}"],
                },
            }
        ),
        actor="fixture",
    )
    revision = save_draft(
        channel,
        project.id,
        SaveEditorialDraft.model_validate(
            {
                "expected_revision": 0,
                "idempotency_key": "shots-revision",
                "draft": {"version": "editorial-draft-v1", "script": []},
            }
        ),
        actor="fixture",
    )
    item = add_project_media_item(
        channel,
        project.id,
        revision.revision,
        request_key="owned",
        actor="fixture",
        kind="video",
        clip_id=clip_id,
    )
    yield channel, other, project.id, item.id, clip_id, source_id, MemoryStore(data)
    engine.dispose()


def build(context):
    channel, _, project, item, _, _, store = context
    return scene_index.index_project_media(channel, project, item, actor="fixture", store=store)


def test_known_shot_measured_boundaries_timebase_sparse_evidence_and_search(indexed_context):
    context = indexed_context
    result = build(context)
    evidence = result["evidence"]
    assert result["status"] == "completed"
    assert result["receipt"]["permission_evidence_refs"] == [f"upload:{context[5]}"]
    assert result["receipt"]["project_id"] == str(context[2])
    assert evidence["decoded_frames"] == 30
    assert evidence["source_origin_tick"] == 5000
    scale = evidence["timebase_numerator"] / evidence["timebase_denominator"]
    assert [shot["start_tick"] * scale for shot in evidence["shots"]] == [0, 1, 2]
    assert evidence["duration_ticks"] * scale == 3
    assert len(evidence["shots"]) == 3
    assert not evidence["ocr_evidence"] and not evidence["speech_evidence"]
    assert evidence["vision_provider_calls"] == 0
    assert evidence["coverage"] == "all_decoded_frames_visual_change_sparse_keyframes"
    channel, _, project, item, _, _, store = context
    for color, expected in (("red", 0), ("green", 1), ("blue", 2)):
        hits = scene_index.search_project_shots(
            channel, project, SearchShotsRequest(item_ids=[item], query=color), store=store
        )
        assert len(hits) == 1
        assert abs(hits[0]["source_in_seconds"] - expected) <= 0.1
        assert hits[0]["source_out_seconds"] <= 3
        assert hits[0]["rights_granted"] is False
    assert (
        scene_index.search_project_shots(
            channel,
            project,
            SearchShotsRequest(item_ids=[item], query="person dancing"),
            store=store,
        )
        == []
    )


def test_durable_exact_replay_does_not_decode_twice(indexed_context, monkeypatch):
    first = build(indexed_context)
    monkeypatch.setattr(scene_index, "_measure", lambda *args: pytest.fail("duplicate decoding"))
    second = build(indexed_context)
    assert second == first
    with db.session_scope() as session:
        assert len(list(session.scalars(select(MediaSceneIndex)))) == 1
        item = session.get(ProjectMediaItem, indexed_context[3])
        assert item.rights_status == "not_assessed" and item.status == "acquired"


def test_changed_bytes_and_changed_canonical_digest_invalidate_index(indexed_context):
    build(indexed_context)
    channel, _, project, item, clip, _, store = indexed_context
    store.data += b"corruption"
    with pytest.raises(ValueError, match="bytes changed"):
        scene_index.get_project_scene_index(channel, project, item, store=store)
    with pytest.raises(ValueError, match="bytes changed"):
        scene_index.search_project_shots(
            channel, project, SearchShotsRequest(item_ids=[item], query="red"), store=store
        )
    with db.session_scope() as session:
        session.get(Clip, clip).sha256 = "f" * 64
    with pytest.raises(ValueError, match="No measured index"):
        scene_index.get_project_scene_index(channel, project, item, store=store)


@pytest.mark.parametrize("state", ["restricted", "denied", "revoked", "expired", "reference_only"])
def test_revoked_or_restricted_rights_stop_before_storage(indexed_context, state):
    channel, _, project, item, _, _, store = indexed_context
    with db.session_scope() as session:
        session.get(ProjectMediaItem, item).rights_status = state
    with pytest.raises(ValueError, match="permission"):
        build(indexed_context)
    assert store.calls == 0
    with pytest.raises(ValueError, match="permission"):
        scene_index.search_project_shots(
            channel, project, SearchShotsRequest(item_ids=[item], query="red"), store=store
        )


def test_channel_and_project_isolation_before_storage(indexed_context):
    channel, other, project, item, _, _, store = indexed_context
    for requested_channel, requested_project in ((other, project), (channel, uuid.uuid4())):
        with pytest.raises(ValueError, match="not found"):
            scene_index.index_project_media(
                requested_channel, requested_project, item, actor="fixture", store=store
            )
        with pytest.raises(ValueError, match="not found"):
            scene_index.search_project_shots(
                requested_channel,
                requested_project,
                SearchShotsRequest(item_ids=[item], query="unobserved action"),
                store=store,
            )
    assert store.calls == 0


def test_permission_revoked_during_read_prevents_publication(indexed_context):
    _, _, _, item, _, _, store = indexed_context

    def revoke():
        with db.session_scope() as session:
            session.get(ProjectMediaItem, item).rights_status = "revoked"

    store.after_read = revoke
    with pytest.raises(ValueError, match="permission"):
        build(indexed_context)
    with db.session_scope() as session:
        row = session.scalar(select(MediaSceneIndex))
        assert row.status == "failed" and not row.evidence


def test_permission_revoked_during_decode_prevents_publication(indexed_context, monkeypatch):
    original = scene_index._measure

    def revoked_after_decode(*args):
        evidence = original(*args)
        with db.session_scope() as session:
            session.get(ProjectMediaItem, indexed_context[3]).rights_status = "revoked"
        return evidence

    monkeypatch.setattr(scene_index, "_measure", revoked_after_decode)
    with pytest.raises(ValueError, match="permission"):
        build(indexed_context)
    with db.session_scope() as session:
        assert session.scalar(select(MediaSceneIndex)).status == "failed"


def test_upload_permission_removed_blocks_existing_index(indexed_context):
    build(indexed_context)
    channel, _, project, item, _, source, store = indexed_context
    with db.session_scope() as session:
        session.get(SourceItem, source).source_metadata = {"channel_profile_id": str(channel)}
    with pytest.raises(ValueError, match="permission"):
        scene_index.get_project_scene_index(channel, project, item, store=store)


def test_size_budget_uses_actual_stream_and_cleans_files(indexed_context, monkeypatch, tmp_path):
    monkeypatch.setattr(tempfile_module := scene_index.tempfile, "tempdir", str(tmp_path))
    monkeypatch.setattr(scene_index, "MAX_INDEX_BYTES", len(indexed_context[-1].data) + 1)
    indexed_context[-1].data *= 2
    with pytest.raises(ValueError, match="byte budget"):
        build(indexed_context)
    assert not list(tmp_path.glob("katcha-scene-index-*"))
    assert tempfile_module.tempdir == str(tmp_path)


def test_worker_timeout_records_failed_and_requires_explicit_retry(indexed_context, monkeypatch):
    original = scene_index.subprocess.run

    def timeout(*args, **kwargs):
        assert kwargs["timeout"] == 90
        raise subprocess.TimeoutExpired(args[0], 90)

    monkeypatch.setattr(scene_index.subprocess, "run", timeout)
    with pytest.raises(ValueError, match="worker budget"):
        build(indexed_context)
    monkeypatch.setattr(scene_index.subprocess, "run", original)
    with pytest.raises(ValueError, match="failed"):
        build(indexed_context)
    channel, _, project, item, _, _, store = indexed_context
    assert (
        scene_index.index_project_media(
            channel, project, item, actor="fixture", store=store, retry_failed=True
        )["status"]
        == "completed"
    )


def test_second_claim_cannot_decode_while_attempt_is_running(indexed_context, monkeypatch):
    original = scene_index._measure

    def nested_attempt(*args):
        with pytest.raises(ValueError, match="unfinished"):
            build(indexed_context)
        return original(*args)

    monkeypatch.setattr(scene_index, "_measure", nested_attempt)
    assert build(indexed_context)["status"] == "completed"


def test_interrupted_attempt_requires_stopped_worker_and_elapsed_window(indexed_context):
    channel, _, project, item, _, _, store = indexed_context
    source = scene_index._source(channel, project, item)
    with db.session_scope() as session:
        session.add(
            MediaSceneIndex(
                id=scene_index._identity(channel, source),
                channel_profile_id=channel,
                clip_id=source.clip_id,
                clip_sha256=source.sha256,
                policy_version=scene_index.POLICY_VERSION,
                status="indexing",
                evidence={},
                receipt={"started_at": datetime.now(UTC).isoformat(), "attempt": 1},
            )
        )
    with pytest.raises(ValueError, match="unfinished"):
        build(indexed_context)
    assert store.calls == 0
    with pytest.raises(ValueError, match="Confirm"):
        scene_index.reconcile_project_scene_index(
            channel, project, item, confirmed_worker_stopped=False, actor="fixture"
        )
    with pytest.raises(ValueError, match="Wait"):
        scene_index.reconcile_project_scene_index(
            channel, project, item, confirmed_worker_stopped=True, actor="fixture"
        )
    with db.session_scope() as session:
        row = session.scalar(select(MediaSceneIndex))
        row.receipt = {
            "started_at": (datetime.now(UTC) - timedelta(seconds=100)).isoformat(),
            "attempt": 1,
        }
    assert (
        scene_index.reconcile_project_scene_index(
            channel, project, item, confirmed_worker_stopped=True, actor="fixture"
        )["status"]
        == "failed"
    )
    assert (
        scene_index.index_project_media(
            channel, project, item, actor="fixture", store=store, retry_failed=True
        )["status"]
        == "completed"
    )


@pytest.mark.parametrize(
    "limit,value,error",
    [
        ("MAX_FRAMES", 2, "frame count"),
        ("MAX_SHOTS", 1, "Shot count"),
        ("MAX_SECONDS", 1, "duration"),
        ("MAX_PIXELS", 10, "dimensions"),
    ],
)
def test_decoder_bounds_are_enforced(known_video, monkeypatch, limit, value, error):
    monkeypatch.setattr(scene_worker, limit, value)
    with pytest.raises(ValueError, match=error):
        scene_worker.measure_video(
            known_video, hashlib.sha256(known_video.read_bytes()).hexdigest()
        )


def test_superseded_attempt_is_fenced_and_cannot_overwrite_new_receipt(
    indexed_context, monkeypatch
):
    original = scene_index._measure

    def replace_attempt(*args):
        evidence = original(*args)
        with db.session_scope() as session:
            row = session.scalar(select(MediaSceneIndex))
            row.receipt = {**row.receipt, "lease_token": "newer-worker"}
        return evidence

    monkeypatch.setattr(scene_index, "_measure", replace_attempt)
    with pytest.raises(ValueError, match="replaced"):
        build(indexed_context)
    with db.session_scope() as session:
        row = session.scalar(select(MediaSceneIndex))
        assert row.status == "indexing" and row.receipt["lease_token"] == "newer-worker"
        assert not row.evidence


def test_search_verification_budget_and_shared_clip_dedupe(indexed_context, monkeypatch):
    build(indexed_context)
    channel, _, project, item, clip, _, store = indexed_context
    with db.session_scope() as session:
        revision = session.get(ProjectMediaItem, item).revision
    second = add_project_media_item(
        channel,
        project,
        revision,
        request_key="alternate-reference",
        actor="fixture",
        kind="video",
        clip_id=clip,
    )
    before = store.calls
    hits = scene_index.search_project_shots(
        channel,
        project,
        SearchShotsRequest(item_ids=[item, second.id], query="red", limit=1),
        store=store,
    )
    assert len(hits) == 1 and store.calls == before + 1
    monkeypatch.setattr(scene_index, "MAX_SEARCH_BYTES", len(store.data) - 1)
    with pytest.raises(ValueError, match="verification byte budget"):
        scene_index.search_project_shots(
            channel, project, SearchShotsRequest(item_ids=[item], query="red"), store=store
        )


def licensed_evidence(context):
    from katcha.acquisition_models import DiscoveryCandidate, RightsAssessment
    from katcha.editorial_models import AcquisitionReceipt, MediaAssetValidation

    channel, _, _, item, clip, source, store = context
    candidate_id, assessment_id, receipt_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    with db.session_scope() as session:
        canonical = session.get(SourceItem, source)
        canonical.platform = "licensed"
        canonical.source_metadata = {"channel_profile_id": str(channel)}
        digest = session.get(Clip, clip).sha256
        session.add(
            DiscoveryCandidate(
                id=candidate_id,
                source_item_id=source,
                adapter_key="fixture",
                source_url=canonical.source_url,
                canonical_url=canonical.canonical_url,
                platform="web",
                candidate_metadata={"channel_profile_id": str(channel)},
            )
        )
        session.add(
            RightsAssessment(
                id=assessment_id,
                discovery_candidate_id=candidate_id,
                version=1,
                production_eligible=True,
                operator_authorized=True,
            )
        )
        session.add(
            AcquisitionReceipt(
                id=receipt_id,
                channel_profile_id=channel,
                candidate_id=candidate_id,
                operation_key="licensed-fixture",
                request_digest="a" * 64,
                status="succeeded",
                authorization_scope={},
                request_time=datetime.now(UTC),
                sha256=digest,
                object_store_key="canonical/owned.mkv",
                bytes_received=len(store.data),
            )
        )
        session.add(
            MediaAssetValidation(
                id=uuid.uuid4(),
                receipt_id=receipt_id,
                channel_profile_id=channel,
                probe_version=1,
                status="validated",
                media_type="video",
                expected_sha256=digest,
                actual_sha256=digest,
                mime="video/x-matroska",
                size_bytes=len(store.data),
                duration_seconds=3,
                width=64,
                height=36,
            )
        )
        media = session.get(ProjectMediaItem, item)
        media.rights_status = "production_cleared"
        media.rights_assessment_id = assessment_id
        media.rights_assessment_version = 1
    return candidate_id, receipt_id


@pytest.mark.parametrize("stale_kind", ["rights", "validation"])
def test_licensed_index_requires_latest_rights_and_validation(indexed_context, stale_kind):
    from katcha.acquisition_models import RightsAssessment
    from katcha.editorial_models import MediaAssetValidation

    candidate, receipt = licensed_evidence(indexed_context)
    assert build(indexed_context)["status"] == "completed"
    channel, _, project, item, clip, _, store = indexed_context
    with db.session_scope() as session:
        if stale_kind == "rights":
            session.add(
                RightsAssessment(
                    id=uuid.uuid4(),
                    discovery_candidate_id=candidate,
                    version=2,
                    production_eligible=False,
                )
            )
        else:
            digest = session.get(Clip, clip).sha256
            session.add(
                MediaAssetValidation(
                    id=uuid.uuid4(),
                    receipt_id=receipt,
                    channel_profile_id=channel,
                    probe_version=2,
                    status="rejected",
                    media_type="video",
                    expected_sha256=digest,
                    actual_sha256=digest,
                    mime="video/x-matroska",
                    size_bytes=len(store.data),
                )
            )
    before = store.calls
    with pytest.raises(ValueError, match="evidence"):
        scene_index.get_project_scene_index(channel, project, item, store=store)
    assert store.calls == before


def test_reference_only_and_unknown_permission_never_inspect(indexed_context):
    channel, _, project, item, _, source, store = indexed_context
    with db.session_scope() as session:
        session.get(SourceItem, source).source_metadata = {"channel_profile_id": str(channel)}
    with pytest.raises(ValueError, match="permission"):
        build(indexed_context)
    with db.session_scope() as session:
        session.get(ProjectMediaItem, item).status = "reference_only"
    with pytest.raises(ValueError, match="acquired canonical"):
        build(indexed_context)
    assert store.calls == 0


def test_api_channel_authorization_and_bounded_inputs(indexed_context, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from katcha.api import editorial_scene_index as routes

    channel, other, project, item, _, _, _ = indexed_context
    app = FastAPI()
    calls = []

    @app.middleware("http")
    async def auth(request, call_next):
        request.state.control_actor = "fixture"
        request.state.control_principal_name = "fixture"
        request.state.control_scopes = {"ai:read"}
        if request.headers.get("x-writer"):
            request.state.control_scopes.add("production:create")
        request.state.control_channel_profile_ids = {str(channel)}
        return await call_next(request)

    def result(*args, **kwargs):
        calls.append(args)
        return {"status": "completed"}

    monkeypatch.setattr(routes, "index_project_media", result)
    monkeypatch.setattr(routes, "get_project_scene_index", result)
    monkeypatch.setattr(routes, "reconcile_project_scene_index", result)
    monkeypatch.setattr(routes, "search_project_shots", lambda *args: [])
    app.include_router(routes.router)
    base = f"/v1/channels/{channel}/editorial-projects/{project}/media-bin"
    with TestClient(app) as client:
        assert client.post(f"{base}/{item}/scene-index", json={}).status_code == 403
        assert client.get(f"{base}/{item}/scene-index").status_code == 200
        assert (
            client.post(
                f"{base}/{item}/scene-index", json={}, headers={"x-writer": "1"}
            ).status_code
            == 200
        )
        assert (
            client.post(
                f"{base}/{item}/scene-index", json={"max_frames": 999999}, headers={"x-writer": "1"}
            ).status_code
            == 422
        )
        assert (
            client.post(
                f"{base}/{item}/scene-index/reconcile", json={"confirmed_worker_stopped": True}
            ).status_code
            == 403
        )
        assert (
            client.post(
                f"{base}/{item}/scene-index/reconcile",
                json={"confirmed_worker_stopped": True},
                headers={"x-writer": "1"},
            ).status_code
            == 200
        )
        assert (
            client.post(
                f"{base}/shot-search", json={"item_ids": [str(item)], "query": "red", "limit": 51}
            ).status_code
            == 422
        )
        before = len(calls)
        assert (
            client.get(f"{base}/{item}/scene-index".replace(str(channel), str(other))).status_code
            == 403
        )
        assert len(calls) == before


def test_schema_rejects_unmeasured_ranges_and_semantic_evidence(indexed_context):
    from katcha.editorial.media_intake.scene_schemas import SceneIndexV1

    evidence = build(indexed_context)["evidence"]
    bad = {**evidence, "duration_ticks": evidence["duration_ticks"] + 10}
    with pytest.raises(ValueError, match="measured decoded duration"):
        SceneIndexV1.model_validate(bad)
    with pytest.raises(ValueError):
        SceneIndexV1.model_validate({**evidence, "ocr_evidence": [{"text": "invented"}]})
    shot = {**evidence["shots"][0], "keyframe_tick": -1}
    with pytest.raises(ValueError):
        SceneIndexV1.model_validate({**evidence, "shots": [shot]})


def test_decoder_concurrency_slot_is_bounded_and_retry_is_explicit(indexed_context):
    assert scene_index._DECODER_SLOT.acquire(blocking=False)
    try:
        with pytest.raises(ValueError, match="inspection is running"):
            build(indexed_context)
    finally:
        scene_index._DECODER_SLOT.release()
    with db.session_scope() as session:
        assert session.scalar(select(MediaSceneIndex)).status == "failed"
    channel, _, project, item, _, _, store = indexed_context
    assert (
        scene_index.index_project_media(
            channel, project, item, actor="fixture", store=store, retry_failed=True
        )["status"]
        == "completed"
    )
