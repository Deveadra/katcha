"""Synthetic offline worker/API/S3 tests. No live provider or deployed-host proof."""

from __future__ import annotations

import base64
import hashlib
import io
import uuid
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs

import httpx
import pytest
import test_acquisition_jobs as foundation
from botocore.exceptions import ClientError
from PIL import Image
from sqlalchemy import select
from test_acquisition_jobs import headers, leased

from katcha import db
from katcha.acquisition.fetch import FetchFailure, fetch_file
from katcha.acquisition.job_contracts import ClaimJob, EnqueueJob, LeaseMessage, ReportJob
from katcha.acquisition.worker import Journal, Worker
from katcha.acquisition_job_models import AcquisitionJob
from katcha.editorial_models import AcquisitionReceipt, MediaAssetValidation
from katcha.integrations.object_streaming import PART_BYTES
from katcha.models import Clip, SourceItem
from katcha.services import acquisition_handoff as handoff
from katcha.services import acquisition_jobs as jobs


@pytest.fixture
def job_db(request):
    yield from foundation.job_db.__wrapped__(
        request.getfixturevalue("monkeypatch"), request.getfixturevalue("tmp_path")
    )


@pytest.fixture
def job_client(job_db, monkeypatch):
    yield from foundation.job_client.__wrapped__(job_db, monkeypatch)


class FakeS3:
    def __init__(self):
        self.uploads, self.objects = {}, {}
        self.grants = []
        self.lose_complete = False

    def create_multipart_upload(self, **kw):
        identity = str(uuid.uuid4())
        self.uploads[identity] = {**kw, "parts": {}}
        return {"UploadId": identity}

    def list_parts(self, **kw):
        parts = self.uploads[kw["UploadId"]]["parts"]
        return {
            "Parts": [
                {"PartNumber": n, "Size": len(data), "ETag": str(n)} for n, data in parts.items()
            ],
            "IsTruncated": False,
        }

    def generate_presigned_url(self, operation, Params, ExpiresIn):
        assert operation == "upload_part" and 1 <= ExpiresIn <= 60
        self.grants.append(Params)
        return f"https://storage.example/upload?grant={len(self.grants) - 1}"

    def storage_request(self, request):
        assert "authorization" not in request.headers
        grant = self.grants[int(parse_qs(request.url.query.decode())["grant"][0])]
        payload = request.read()
        assert request.headers["content-md5"] == grant["ContentMD5"]
        assert base64.b64encode(hashlib.md5(payload).digest()).decode() == grant["ContentMD5"]
        assert len(payload) == grant["ContentLength"]
        self.uploads[grant["UploadId"]]["parts"][grant["PartNumber"]] = payload
        return httpx.Response(200, headers={"etag": "part-ok"})

    def complete_multipart_upload(self, **kw):
        upload = self.uploads[kw["UploadId"]]
        content = b"".join(upload["parts"][n] for n in sorted(upload["parts"]))
        self.objects[kw["Key"]] = {"Body": content, "Metadata": upload["Metadata"]}
        del self.uploads[kw["UploadId"]]
        if self.lose_complete:
            self.lose_complete = False
            raise RuntimeError("synthetic completion ACK lost")

    def head_object(self, **kw):
        if kw["Key"] not in self.objects:
            raise ClientError({"Error": {"Code": "NoSuchKey"}}, "HeadObject")
        item = self.objects[kw["Key"]]
        return {"ContentLength": len(item["Body"]), "Metadata": item["Metadata"]}

    def get_object(self, **kw):
        content = self.objects[kw["Key"]]["Body"]
        start, end = [int(s) for s in kw["Range"].removeprefix("bytes=").split("-")]
        return {
            "Body": io.BytesIO(content[start : end + 1]),
            "ContentRange": f"bytes {start}-{end}/{len(content)}",
        }

    def abort_multipart_upload(self, **kw):
        if self.uploads.pop(kw["UploadId"], None) is None:
            raise ClientError({"Error": {"Code": "NoSuchUpload"}}, "AbortMultipartUpload")


class FakeStore:
    def __init__(self):
        from katcha.config import Settings

        self.settings = Settings(_env_file=None)
        self.client = FakeS3()

    def iter_bytes(self, key, chunk_size):
        data = self.client.objects[key]["Body"]
        for pos in range(0, len(data), chunk_size):
            yield data[pos : pos + chunk_size]

    def copy(self, source, destination, **kwargs):
        self.client.objects[destination] = dict(self.client.objects[source])

    def delete(self, key):
        self.client.objects.pop(key, None)


@pytest.fixture
def store(monkeypatch):
    value = FakeStore()
    monkeypatch.setattr(handoff, "ObjectStore", lambda: value)
    return value


def png():
    output = io.BytesIO()
    Image.new("RGB", (32, 24), (10, 80, 120)).save(output, format="PNG")
    return output.getvalue()


def transfer(fixture, store, data=None):
    data = data or png()
    row = jobs.enqueue_job(fixture[0], EnqueueJob(receipt_id=fixture[2], media_type="image"))
    row, _, _ = jobs.claim_job(fixture[0], row.id, "worker-a", ClaimJob(claim_id=uuid.uuid4()))
    identity = LeaseMessage(generation=row.generation, claim_id=row.claim_id)
    handoff.prepare_upload(
        fixture[0],
        row.id,
        "worker-a",
        ReportJob(
            **identity.model_dump(), sha256=hashlib.sha256(data).hexdigest(), size_bytes=len(data)
        ),
        store=store,
    )
    for pos in range(0, len(data), PART_BYTES):
        part = data[pos : pos + PART_BYTES]
        md5 = base64.b64encode(hashlib.md5(part).digest()).decode()
        grant = handoff.authorize_part(
            fixture[0], row.id, "worker-a", identity, pos // PART_BYTES + 1, md5, store=store
        )
        with httpx.Client(transport=httpx.MockTransport(store.client.storage_request)) as client:
            client.put(grant.url, headers=grant.headers, content=part).raise_for_status()
    return row, identity


def test_worker_to_api_to_storage_to_verified_channel_clip(
    job_db, job_client, store, tmp_path, monkeypatch
):
    from katcha.acquisition import worker as module

    client, _, root = job_client
    data = png()
    response = client.post(
        root, headers=headers(), json={"receipt_id": str(job_db[2]), "media_type": "image"}
    )
    assert response.status_code == 200, response.text
    job_id = response.json()["id"]
    calls = []

    def source(request):
        assert "authorization" not in request.headers
        calls.append(request)
        return httpx.Response(
            200,
            content=data,
            headers={
                "Content-Length": str(len(data)),
                "Content-Type": "image/png",
                "ETag": '"fixture-v1"',
            },
        )

    original = fetch_file
    monkeypatch.setattr(
        module,
        "fetch_file",
        lambda *a, **kw: original(
            *a, **kw, client_factory=lambda: httpx.Client(transport=httpx.MockTransport(source))
        ),
    )
    worker = Worker(
        "https://control.example",
        "worker-a-fixture-token-001",
        [str(job_db[0])],
        tmp_path / "worker",
        client=client,
        storage_client=httpx.Client(transport=httpx.MockTransport(store.client.storage_request)),
    )
    worker.once()
    assert len(calls) == 1
    assert jobs.get_job(job_db[0], uuid.UUID(job_id)).status == "awaiting_validation"
    with db.session_scope() as session:
        assert session.get(AcquisitionReceipt, job_db[2]).status == "acquiring"
        assert session.scalar(select(Clip)) is None
    # Workers cannot self-validate, promote rights, or invoke operator controls.
    assert client.post(f"{root}/{job_id}/validate", headers=headers("worker-a")).status_code == 403
    accepted = client.post(f"{root}/{job_id}/validate", headers=headers())
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["status"] == "completed" and accepted.json()["media_verified"]
    with db.session_scope() as session:
        receipt = session.get(AcquisitionReceipt, job_db[2])
        validation = session.scalar(select(MediaAssetValidation))
        source = session.get(SourceItem, uuid.UUID(accepted.json()["source_item_id"]))
        clip = session.get(Clip, source.clip_id)
        assert receipt.status == "succeeded" and validation.status == "validated"
        assert receipt.sha256 == clip.sha256 == hashlib.sha256(data).hexdigest()
        assert source.status == "ready" and source.source_metadata["channel_profile_id"] == str(
            job_db[0]
        )
        assert "production_cleared" not in str(source.source_metadata)
    # Re-running worker and validation repeats neither provider access nor Clip creation.
    worker.once()
    assert len(calls) == 1
    assert (
        client.post(f"{root}/{job_id}/validate", headers=headers()).json()["status"] == "completed"
    )
    with db.session_scope() as session:
        assert len(list(session.scalars(select(Clip)))) == 1
        assert len(list(session.scalars(select(MediaAssetValidation)))) == 1


def test_completion_ack_loss_reconciles_existing_object_without_reupload(job_db, store):
    row, identity = transfer(job_db, store)
    store.client.lose_complete = True
    with pytest.raises(RuntimeError, match="ACK lost"):
        handoff.complete_upload(job_db[0], row.id, "worker-a", identity, store=store)
    assert jobs.get_job(job_db[0], row.id).status == "leased"
    result = handoff.complete_upload(job_db[0], row.id, "worker-a", identity, store=store)
    assert result.status == "awaiting_validation"
    assert (
        handoff.complete_upload(job_db[0], row.id, "worker-a", identity, store=store).status
        == result.status
    )
    assert len(store.client.grants) == 1


def test_cancel_fences_upload_completion_and_validation(job_db, store):
    row, identity = transfer(job_db, store)
    jobs.cancel_job(job_db[0], row.id)
    with pytest.raises(jobs.JobConflict):
        handoff.complete_upload(job_db[0], row.id, "worker-a", identity, store=store)
    with pytest.raises(jobs.JobConflict):
        handoff.finalize_job(job_db[0], row.id, store=store)
    assert not store.client.objects
    handoff.cleanup_transfer(job_db[0], row.id, store=store)
    assert not store.client.uploads


def test_tampered_bytes_fail_independent_validation_without_clip(job_db, store):
    row, identity = transfer(job_db, store)
    handoff.complete_upload(job_db[0], row.id, "worker-a", identity, store=store)
    store.client.objects[jobs.staging_key(row)]["Body"] += b"tampered"
    assert handoff.finalize_job(job_db[0], row.id, store=store).status == "failed"
    with db.session_scope() as session:
        assert session.scalar(select(Clip)) is None
        assert session.get(AcquisitionReceipt, row.receipt_id).status == "failed"


def test_container_shaped_invalid_content_is_rejected(job_db, store):
    row, identity = transfer(job_db, store, b"this is not an image")
    handoff.complete_upload(job_db[0], row.id, "worker-a", identity, store=store)
    assert handoff.finalize_job(job_db[0], row.id, store=store).status == "failed"


def test_part_hash_is_frozen_and_cross_worker_cannot_get_grant(job_db, store):
    row, identity = transfer(job_db, store)
    with pytest.raises(jobs.JobConflict, match="different checksum"):
        handoff.authorize_part(
            job_db[0],
            row.id,
            "worker-a",
            identity,
            1,
            base64.b64encode(b"x" * 16).decode(),
            store=store,
        )
    with pytest.raises(jobs.JobConflict):
        handoff.authorize_part(
            job_db[0],
            row.id,
            "worker-b",
            identity,
            1,
            base64.b64encode(b"x" * 16).decode(),
            store=store,
        )
    with pytest.raises(jobs.JobNotFound):
        handoff.prepare_upload(
            job_db[1],
            row.id,
            "worker-a",
            ReportJob(**identity.model_dump(), sha256="a" * 64, size_bytes=1),
            store=store,
        )


def test_known_upload_recovers_expired_lease_and_preserves_parts(job_db, store):
    row, identity = transfer(job_db, store)
    with db.session_scope() as session:
        session.get(AcquisitionJob, row.id).lease_expires_at = datetime.now(UTC) - timedelta(
            seconds=1
        )
    message = ReportJob(
        **identity.model_dump(), sha256=hashlib.sha256(png()).hexdigest(), size_bytes=len(png())
    )
    with pytest.raises(jobs.JobConflict):
        handoff.prepare_upload(job_db[0], row.id, "worker-a", message, store=store)
    recovered, _, _ = handoff.recover_upload(job_db[0], row.id, "worker-a", message)
    assert recovered.generation == row.generation
    resumed = handoff.prepare_upload(job_db[0], row.id, "worker-a", message, store=store)
    assert resumed.uploaded_parts == [1]
    with pytest.raises(jobs.JobConflict):
        handoff.recover_upload(job_db[0], row.id, "worker-b", message)


def test_grants_support_3gb_plus_bounds_without_allocating_media(job_db, store):
    row, identity = leased(job_db)
    size = 3 * 1024**3 + 17
    handoff.prepare_upload(
        job_db[0],
        row.id,
        "worker-a",
        ReportJob(**identity, sha256="a" * 64, size_bytes=size),
        store=store,
    )
    identity = LeaseMessage(**identity)
    handoff.authorize_part(
        job_db[0],
        row.id,
        "worker-a",
        identity,
        385,
        base64.b64encode(b"x" * 16).decode(),
        store=store,
    )
    assert store.client.grants[-1]["ContentLength"] == 17
    with pytest.raises(jobs.JobConflict):
        handoff.authorize_part(
            job_db[0],
            row.id,
            "worker-a",
            identity,
            386,
            base64.b64encode(b"x" * 16).decode(),
            store=store,
        )


def test_permission_revocation_after_transfer_blocks_promotion(job_db, store):
    row, identity = transfer(job_db, store)
    handoff.complete_upload(job_db[0], row.id, "worker-a", identity, store=store)
    with db.session_scope() as session:
        receipt = session.get(AcquisitionReceipt, row.receipt_id)
        receipt.authorization_scope = {
            **receipt.authorization_scope,
            "expires_at": "2000-01-01T00:00:00Z",
        }
    with pytest.raises(jobs.JobConflict):
        handoff.finalize_job(job_db[0], row.id, store=store)
    with db.session_scope() as session:
        assert session.scalar(select(Clip)) is None


def test_https_resume_reuses_exact_strong_etag_representation(tmp_path):
    permission = dict(
        provider="fixture",
        operations=["download"],
        allowed_hosts=["media.example"],
        evidence_scope="synthetic fixture",
    )
    path = tmp_path / "media"
    path.write_bytes(b"abc")
    checkpoint = {"etag": '"stable"', "total": 8}

    def respond(request):
        assert request.headers["range"] == "bytes=3-"
        assert request.headers["if-match"] == '"stable"'
        return httpx.Response(
            206, content=b"defgh", headers={"Content-Range": "bytes 3-7/8", "ETag": '"stable"'}
        )

    fetch_file(
        "https://media.example/file",
        permission,
        path,
        10,
        checkpoint,
        lambda _: None,
        client_factory=lambda: httpx.Client(transport=httpx.MockTransport(respond)),
    )
    assert path.read_bytes() == b"abcdefgh"


def test_resume_never_overwrites_partial_when_server_ignores_range(tmp_path):
    permission = dict(
        provider="fixture",
        operations=["download"],
        allowed_hosts=["media.example"],
        evidence_scope="synthetic fixture",
    )
    path = tmp_path / "media"
    path.write_bytes(b"abc")
    with pytest.raises(FetchFailure) as error:
        fetch_file(
            "https://media.example/file",
            permission,
            path,
            10,
            {"etag": '"v1"', "total": 8},
            lambda _: None,
            client_factory=lambda: httpx.Client(
                transport=httpx.MockTransport(
                    lambda _: httpx.Response(200, content=b"abcdefgh", headers={"ETag": '"v1"'})
                )
            ),
        )
    assert error.value.code == "outcome_unknown"
    assert path.read_bytes() == b"abc"


def test_journal_persists_claim_before_dispatch_and_is_private(tmp_path):
    channel, job_id = str(uuid.uuid4()), str(uuid.uuid4())
    journal = Journal(tmp_path, channel, job_id)
    claim = journal.state["claim_id"]
    assert Journal(tmp_path, channel, job_id).state["claim_id"] == claim
    assert journal.path.stat().st_mode & 0o077 == 0


@pytest.mark.parametrize("quarantine", [False, True])
def test_worker_restart_resumes_uploaded_parts_without_provider_call(
    job_db, job_client, store, tmp_path, monkeypatch, quarantine
):
    from katcha.acquisition import worker as module

    row, identity = transfer(job_db, store)
    with db.session_scope() as session:
        session.get(AcquisitionJob, row.id).lease_expires_at = datetime.now(UTC) - timedelta(
            seconds=1
        )
    if quarantine:
        jobs.reconcile_expired_job(job_db[0], row.id)
    journal = Journal(tmp_path / "worker", str(job_db[0]), str(row.id))
    (journal.directory / "media.part").write_bytes(png())
    journal.update(
        phase="uploading",
        filename="media.part",
        claim_id=str(identity.claim_id),
        generation=identity.generation,
        sha256=hashlib.sha256(png()).hexdigest(),
        size_bytes=len(png()),
        bytes_received=len(png()),
    )
    monkeypatch.setattr(module, "fetch_file", lambda *a, **kw: pytest.fail("provider replayed"))
    worker = Worker(
        "https://control.example",
        "worker-a-fixture-token-001",
        [str(job_db[0])],
        tmp_path / "worker",
        client=job_client[0],
        storage_client=httpx.Client(
            transport=httpx.MockTransport(lambda _: pytest.fail("acknowledged part re-uploaded"))
        ),
    )
    worker.once()
    assert jobs.get_job(job_db[0], row.id).status == "awaiting_validation"
    assert len(store.client.grants) == 1


def test_cancel_during_probe_prevents_final_asset_promotion(job_db, store, monkeypatch):
    row, identity = transfer(job_db, store)
    handoff.complete_upload(job_db[0], row.id, "worker-a", identity, store=store)
    original = handoff.probe_object

    def cancel(*args):
        result = original(*args)
        jobs.cancel_job(job_db[0], row.id)
        return result

    monkeypatch.setattr(handoff, "probe_object", cancel)
    with pytest.raises(jobs.JobConflict):
        handoff.finalize_job(job_db[0], row.id, store=store)
    with db.session_scope() as session:
        assert session.scalar(select(Clip)) is None
        assert session.get(AcquisitionReceipt, row.receipt_id).status == "unknown"


def test_primary_probes_and_decodes_real_video(job_db, store, tmp_path):
    import av

    path = tmp_path / "tiny.mp4"
    with av.open(str(path), "w") as container:
        stream = container.add_stream("mpeg4", rate=24)
        stream.width = stream.height = 16
        stream.pix_fmt = "yuv420p"
        for i in range(3):
            frame = av.VideoFrame(16, 16, "yuv420p")
            for plane in frame.planes:
                plane.update(bytes(plane.buffer_size))
            frame.pts = i
            for packet in stream.encode(frame):
                container.mux(packet)
        for packet in stream.encode():
            container.mux(packet)
    row = jobs.enqueue_job(job_db[0], EnqueueJob(receipt_id=job_db[2], media_type="video"))
    row, _, _ = jobs.claim_job(job_db[0], row.id, "worker-a", ClaimJob(claim_id=uuid.uuid4()))
    identity = LeaseMessage(generation=row.generation, claim_id=row.claim_id)
    data = path.read_bytes()
    handoff.prepare_upload(
        job_db[0],
        row.id,
        "worker-a",
        ReportJob(
            **identity.model_dump(), sha256=hashlib.sha256(data).hexdigest(), size_bytes=len(data)
        ),
        store=store,
    )
    grant = handoff.authorize_part(
        job_db[0],
        row.id,
        "worker-a",
        identity,
        1,
        base64.b64encode(hashlib.md5(data).digest()).decode(),
        store=store,
    )
    with httpx.Client(transport=httpx.MockTransport(store.client.storage_request)) as client:
        client.put(grant.url, content=data, headers=grant.headers).raise_for_status()
    handoff.complete_upload(job_db[0], row.id, "worker-a", identity, store=store)
    result = handoff.finalize_job(job_db[0], row.id, store=store)
    assert result.status == "completed"
    with db.session_scope() as session:
        clip = session.scalar(select(Clip))
        assert clip.width == clip.height == 16 and clip.duration_seconds > 0
        assert clip.storage_key.startswith("media/sha256/")


def test_every_extractor_request_is_checked_and_uses_isolated_transport(tmp_path, monkeypatch):
    from yt_dlp import YoutubeDL

    from katcha.acquisition import fetch

    requested = []

    def respond(request):
        assert "authorization" not in request.headers
        requested.append(str(request.url))
        return httpx.Response(200, content=b"synthetic direct payload")

    monkeypatch.setattr(fetch, "PublicDestinationTransport", lambda _: httpx.MockTransport(respond))

    def extract(self, url, download):
        with pytest.raises(FetchFailure):
            self.urlopen("http://media.example/insecure")
        with pytest.raises(FetchFailure):
            self.urlopen("https://internal.example/metadata")
        response = self.urlopen("https://media.example/allowed")
        data = response.read()
        response.close()
        (tmp_path / "media.mp4").write_bytes(data)
        return {}

    monkeypatch.setattr(YoutubeDL, "extract_info", extract)
    result = fetch.fetch_extracted(
        "https://www.youtube.com/watch?v=fixture",
        {
            "provider": "fixture",
            "operations": ["download"],
            "evidence_scope": "synthetic fixture",
            "allowed_hosts": ["www.youtube.com", "media.example"],
        },
        tmp_path,
        100,
        lambda _: None,
    )
    assert result.read_bytes() == b"synthetic direct payload"
    assert requested == ["https://media.example/allowed"]


def test_additive_migration_matches_models_and_preserves_wide_counters(tmp_path):
    import importlib.util
    from pathlib import Path

    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import (
        BigInteger,
        Column,
        Integer,
        MetaData,
        Table,
        Uuid,
        create_engine,
        inspect,
    )

    from katcha.acquisition_job_models import AcquisitionTransfer

    def load(name):
        spec = importlib.util.spec_from_file_location(name, Path(f"migrations/versions/{name}.py"))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    original, current = load("0065_acquisition_jobs"), load("0066_acquisition_handoff")
    engine = create_engine(f"sqlite:///{tmp_path / 'migration.sqlite'}")
    metadata = MetaData()
    Table(
        "acquisition_receipts",
        metadata,
        Column("id", Uuid, primary_key=True),
        Column("channel_profile_id", Uuid),
        Column("bytes_received", Integer),
    )
    Table(
        "media_asset_validations",
        metadata,
        Column("id", Uuid, primary_key=True),
        Column("size_bytes", Integer),
    )
    metadata.create_all(engine)
    with engine.begin() as connection:
        operations = Operations(MigrationContext.configure(connection))
        original.op = current.op = operations
        original.upgrade()
        current.upgrade()
        inspector = inspect(connection)
        assert {c["name"] for c in inspector.get_columns("acquisition_jobs")} == set(
            AcquisitionJob.__table__.columns.keys()
        )
        assert {c["name"] for c in inspector.get_columns("acquisition_transfers")} == set(
            AcquisitionTransfer.__table__.columns.keys()
        )
        assert isinstance(
            next(
                c
                for c in inspector.get_columns("acquisition_receipts")
                if c["name"] == "bytes_received"
            )["type"],
            BigInteger,
        )
        current.downgrade()
        assert "acquisition_transfers" not in inspect(connection).get_table_names()
    engine.dispose()


@pytest.mark.asyncio
async def test_temporal_workflow_is_sandbox_compatible():
    from temporalio import workflow
    from temporalio.worker.workflow_sandbox import SandboxedWorkflowRunner

    from katcha.orchestration.acquisition_workflows import RemoteAcquisitionWorkflow

    SandboxedWorkflowRunner().prepare_workflow(
        workflow._Definition.must_from_class(RemoteAcquisitionWorkflow)
    )


@pytest.mark.asyncio
async def test_remote_workflow_waits_then_accepts_verified_handoff(monkeypatch):
    from katcha.orchestration import acquisition_workflows as module

    outcomes = iter(
        [{"status": "leased"}, {"status": "awaiting_validation"}, {"status": "completed"}]
    )
    seen = []

    async def execute(name, **kwargs):
        seen.append((name, kwargs["args"]))
        return next(outcomes)

    async def sleep(_):
        pass

    monkeypatch.setattr(module.workflow, "execute_activity", execute)
    monkeypatch.setattr(module.workflow, "sleep", sleep)
    monkeypatch.setattr(module.workflow, "patched", lambda _: True)
    assert (await module.RemoteAcquisitionWorkflow().run("channel", "job"))["status"] == "completed"
    assert seen == [("resource_queued_reconcile_remote_acquisition", ["channel", "job"])] * 3


def test_ingest_automatically_dispatches_authorized_receipt_once(job_db, monkeypatch):
    from katcha.acquisition_models import DiscoveryCandidate
    from katcha.config import Settings
    from katcha.orchestration import acquisition_activities as module

    with db.session_scope() as session:
        receipt = session.get(AcquisitionReceipt, job_db[2])
        candidate = session.get(DiscoveryCandidate, receipt.candidate_id)
        source = SourceItem(
            id=uuid.uuid4(),
            source_url=candidate.canonical_url,
            canonical_url=candidate.canonical_url,
            platform="web",
            source_metadata={"channel_profile_id": str(job_db[0])},
        )
        session.add(source)
        candidate.source_item_id = source.id
        source_id = str(source.id)
    monkeypatch.setattr(
        module, "get_settings", lambda: Settings(_env_file=None, remote_acquisition_enabled=True)
    )
    first = module.remote_source_acquisition(source_id)
    assert module.remote_source_acquisition(source_id) == first
    assert jobs.get_job(job_db[0], uuid.UUID(first["job_id"])).source_item_id == uuid.UUID(
        source_id
    )
    with db.session_scope() as session:
        assert len(list(session.scalars(select(AcquisitionJob)))) == 1


def test_missing_download_permission_never_falls_back_to_local_acquisition(job_db, monkeypatch):
    from temporalio.exceptions import ApplicationError

    from katcha.config import Settings
    from katcha.orchestration import acquisition_activities as module

    with db.session_scope() as session:
        source = SourceItem(
            id=uuid.uuid4(),
            source_url="https://media.example/no-consent",
            canonical_url="https://media.example/no-consent",
            platform="web",
            source_metadata={"channel_profile_id": str(job_db[0])},
        )
        session.add(source)
        source_id = str(source.id)
    monkeypatch.setattr(
        module, "get_settings", lambda: Settings(_env_file=None, remote_acquisition_enabled=True)
    )
    with pytest.raises(ApplicationError, match="Approve a channel-owned"):
        module.remote_source_acquisition(source_id)
    assert jobs.list_jobs(job_db[0]) == []


def test_media_probe_refuses_external_playlist_formats(job_db, store):
    playlist = b"#EXTM3U\n#EXT-X-TARGETDURATION:5\n#EXTINF:5,\nhttp://127.0.0.1/private.ts\n"
    store.client.objects["fixture-playlist"] = {"Body": playlist, "Metadata": {}}
    import av

    with pytest.raises(av.FFmpegError):
        handoff.probe_object(store, "fixture-playlist", len(playlist), "video")


def test_quarantined_known_upload_recovers_with_new_generation_and_old_parts(job_db, store):
    row, old = transfer(job_db, store)
    with db.session_scope() as session:
        session.get(AcquisitionJob, row.id).lease_expires_at = datetime.now(UTC) - timedelta(
            minutes=10
        )
    quarantined = jobs.reconcile_expired_job(job_db[0], row.id)
    assert quarantined.status == "recovery_required"
    message = ReportJob(
        **old.model_dump(), sha256=hashlib.sha256(png()).hexdigest(), size_bytes=len(png())
    )
    recovered, _, _ = handoff.recover_upload(job_db[0], row.id, "worker-a", message)
    assert recovered.generation > quarantined.generation
    identity = LeaseMessage(claim_id=old.claim_id, generation=recovered.generation)
    with pytest.raises(jobs.JobConflict):
        handoff.complete_upload(job_db[0], row.id, "worker-a", old, store=store)
    manifest = handoff.prepare_upload(
        job_db[0],
        row.id,
        "worker-a",
        ReportJob(**identity.model_dump(), sha256=message.sha256, size_bytes=message.size_bytes),
        store=store,
    )
    assert manifest.uploaded_parts == [1]
    result = handoff.complete_upload(job_db[0], row.id, "worker-a", identity, store=store)
    assert result.pending_result["object_store_key"] == jobs.staging_key(row)
    assert handoff.finalize_job(job_db[0], row.id, store=store).status == "completed"


def test_unknown_execution_requires_explicit_operator_release_before_new_attempt(
    job_db, job_client
):
    from katcha.editorial.media_intake.contracts import ProviderPermissionV1
    from katcha.editorial.media_intake.receipts import start_acquisition

    row, _ = leased(job_db)
    with db.session_scope() as session:
        session.get(AcquisitionJob, row.id).lease_expires_at = datetime.now(UTC) - timedelta(
            minutes=10
        )
    jobs.reconcile_expired_job(job_db[0], row.id)
    with db.session_scope() as session:
        old = session.get(AcquisitionReceipt, row.receipt_id)
        candidate_id, url = old.candidate_id, old.fetch_url_if_allowed
        permission = ProviderPermissionV1.model_validate(
            {
                k: v
                for k, v in old.authorization_scope.items()
                if k in ProviderPermissionV1.model_fields
            }
        )
    fresh = start_acquisition(
        job_db[0],
        candidate_id,
        operation_key="new-explicit-operator-attempt",
        permission=permission,
        fetch_url_if_allowed=url,
        actor="operator",
    )
    with pytest.raises(jobs.JobConflict, match="Reconcile"):
        jobs.enqueue_job(job_db[0], EnqueueJob(receipt_id=fresh.id))
    client, _, root = job_client
    path = f"{root}/{row.id}/release-source"
    assert (
        client.post(
            path, headers=headers("worker-a"), json={"acknowledge_unknown_outcome": True}
        ).status_code
        == 403
    )
    assert (
        client.post(
            path, headers=headers(), json={"acknowledge_unknown_outcome": False}
        ).status_code
        == 422
    )
    assert (
        client.post(path, headers=headers(), json={"acknowledge_unknown_outcome": True}).status_code
        == 200
    )
    replacement = jobs.enqueue_job(job_db[0], EnqueueJob(receipt_id=fresh.id))
    assert replacement.source_item_id == row.source_item_id
    with db.session_scope() as session:
        assert session.get(AcquisitionReceipt, row.receipt_id).status == "unknown"


def test_real_s3_part_signature_binds_checksum_and_length():
    from katcha.config import Settings
    from katcha.integrations.storage import ObjectStore

    store = ObjectStore(
        Settings(
            _env_file=None,
            s3_region="auto",
            s3_endpoint_url="https://storage.example",
            s3_access_key="fixture-access",
            s3_secret_key="fixture-secret",
        )
    )
    url = store.client.generate_presigned_url(
        "upload_part",
        Params={
            "Bucket": "fixture",
            "Key": "only/this/job",
            "UploadId": "fixture-upload",
            "PartNumber": 1,
            "ContentLength": 17,
            "ContentMD5": base64.b64encode(b"x" * 16).decode(),
        },
        ExpiresIn=60,
    )
    query = parse_qs(httpx.URL(url).query.decode())
    assert set(query["X-Amz-SignedHeaders"][0].split(";")) >= {
        "content-length",
        "content-md5",
        "host",
    }


def test_supervised_queue_only_mode_preserves_unlinked_ingest(job_db, monkeypatch):
    from katcha.config import Settings
    from katcha.orchestration import acquisition_activities as module

    settings = Settings(
        _env_file=None,
        remote_acquisition_enabled=True,
        remote_acquisition_auto_dispatch_enabled=False,
    )
    monkeypatch.setattr(module, "get_settings", lambda: settings)
    with db.session_scope() as session:
        source = SourceItem(
            id=uuid.uuid4(),
            source_url="https://media.example/unqueued",
            canonical_url="https://media.example/unqueued",
            platform="web",
            source_metadata={"channel_profile_id": str(job_db[0])},
        )
        session.add(source)
        source_id = str(source.id)
    assert module.remote_source_acquisition(source_id) == {}
    row = foundation.queued(job_db)
    assert module.remote_source_acquisition(str(row.source_item_id)) == {
        "job_id": str(row.id),
        "channel_id": str(job_db[0]),
    }


def test_operator_retries_post_acceptance_staging_cleanup_without_deleting_asset(job_db, store):
    row, identity = transfer(job_db, store)
    handoff.complete_upload(job_db[0], row.id, "worker-a", identity, store=store)
    original_delete = store.delete

    def interrupted(_key):
        raise OSError("synthetic staging delete outage")

    store.delete = interrupted
    assert handoff.finalize_job(job_db[0], row.id, store=store).status == "completed"
    store.delete = original_delete
    assert any(key.startswith("acquisition-staging/") for key in store.client.objects)
    handoff.cleanup_transfer(job_db[0], row.id, store=store)
    assert not any(key.startswith("acquisition-staging/") for key in store.client.objects)
    assert any(key.startswith("media/sha256/") for key in store.client.objects)
    with db.session_scope() as session:
        assert session.get(AcquisitionReceipt, job_db[2]).status == "succeeded"


@pytest.mark.asyncio
async def test_recovered_upload_dispatches_after_previous_quarantine_workflow_closed(
    job_db, store, monkeypatch
):
    import asyncio

    from temporalio.common import WorkflowIDReusePolicy
    from temporalio.exceptions import WorkflowAlreadyStartedError

    from katcha.config import Settings
    from katcha.orchestration import acquisition_activities as module

    row, identity = transfer(job_db, store)
    foundation.expire(row)
    jobs.reconcile_expired_job(job_db[0], row.id)
    handoff.recover_upload(
        job_db[0],
        row.id,
        "worker-a",
        ReportJob(
            **identity.model_dump(),
            sha256=hashlib.sha256(png()).hexdigest(),
            size_bytes=len(png()),
        ),
    )
    started = []

    class ClosedQuarantineWorkflowClient:
        async def start_workflow(self, name, **kwargs):
            if kwargs["id_reuse_policy"] != WorkflowIDReusePolicy.ALLOW_DUPLICATE:
                raise WorkflowAlreadyStartedError(kwargs["id"], name)
            started.append(kwargs["id"])

    async def finish_pass(_seconds):
        raise asyncio.CancelledError

    monkeypatch.setattr(module.asyncio, "sleep", finish_pass)
    with pytest.raises(asyncio.CancelledError):
        await module.supervise_acquisitions(
            ClosedQuarantineWorkflowClient(), Settings(_env_file=None)
        )
    assert started == [f"acquisition-{row.id}"]
