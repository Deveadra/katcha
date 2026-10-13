from __future__ import annotations

import io
import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.requests import Request

from katcha import db
from katcha.api.editorial_source_multipart import _auth
from katcha.api.main import app
from katcha.config import Settings
from katcha.editorial import source_multipart as uploads
from katcha.intelligence_models import ChannelProfile
from katcha.models import Clip, SourceItem


class MemoryS3:
    def __init__(self):
        self.uploads = []
        self.items = {}
        self.upload_parts = []
        self.aborted = []
        self.copied = []
        self.completed = []
        self.signed_parts = []

    def create_multipart_upload(self, **kwargs):
        self.uploads.append(kwargs)
        return {"UploadId": "opaque-r2-upload-id"}

    def generate_presigned_url(self, operation, Params, ExpiresIn, **kwargs):
        assert ExpiresIn <= 600
        if operation == "upload_part":
            self.signed_parts.append(dict(Params))
        return "https://r2.example.test/signed/" + operation

    def list_parts(self, **kwargs):
        return {"Parts": list(self.upload_parts), "IsTruncated": False}

    def complete_multipart_upload(self, **kwargs):
        self.completed.append(kwargs)
        self.items[kwargs["Key"]] = b"media"

    def copy_object(self, **kwargs):
        self.copied.append(kwargs)
        self.items[kwargs["Key"]] = self.items[kwargs["CopySource"]["Key"]]

    def get_object(self, **kwargs):
        value = self.items[kwargs["Key"]]
        return {"ContentLength": len(value), "Body": io.BytesIO(value)}

    def abort_multipart_upload(self, **kwargs):
        self.aborted.append(kwargs)


class MemoryStore:
    @staticmethod
    def raw_key(sha256, extension):
        return f"raw/{sha256[:2]}/{sha256}.{extension}"

    def __init__(self):
        self.settings = SimpleNamespace(
            s3_bucket="katcha-media-prod",
            s3_browser_endpoint_url="https://r2.example.test",
            env="production",
        )
        self.client = MemoryS3()
        self.deleted = []

    def _client(self, _endpoint):
        return self.client

    def exists(self, key):
        return key in self.client.items

    def delete(self, key):
        self.deleted.append(key)
        self.client.items.pop(key, None)


@pytest.fixture
def upload_db(monkeypatch):
    db.load_model_metadata()
    engine = create_engine(
        "sqlite://", poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    db.Base.metadata.create_all(engine)
    monkeypatch.setattr(
        db, "SessionLocal", sessionmaker(bind=engine, expire_on_commit=False)
    )
    channel = uuid.uuid4()
    other = uuid.uuid4()
    with db.session_scope() as session:
        for value in (channel, other):
            session.add(ChannelProfile(
                id=value, youtube_connection_id=uuid.uuid4(),
                status="active", timezone="UTC", profile_metadata={},
            ))
    store = MemoryStore()
    monkeypatch.setattr(uploads, "get_settings", lambda: store.settings)
    monkeypatch.setattr(uploads, "ObjectStore", lambda settings=None: store)
    monkeypatch.setattr(uploads, "assert_mutation_authority", lambda *a, **k: None)
    yield channel, other, store
    engine.dispose()


def begin(channel, *, size_bytes=3 * 1024**3, identity="original", **overrides):
    params = dict(
        filename="4k-trailer.mp4", title="4K Official Trailer",
        content_type="video/mp4", size_bytes=size_bytes,
        permitted_use=True, idempotency_key=identity,
    )
    params.update(overrides)
    return uploads.initiate(channel, **params)


def test_three_gib_browser_upload_is_scoped_resumable_and_never_buffers_media(upload_db):
    channel, other, store = upload_db
    started = begin(channel)
    assert started["state"] == "uploading"
    assert started["part_count"] == 48
    assert started["part_bytes"] == 64 * 1024**2
    assert len(store.client.uploads) == 1

    replay = begin(channel)
    assert replay == started
    assert len(store.client.uploads) == 1
    signed = uploads.sign_part(channel, uuid.UUID(started["source_id"]), 48)
    assert signed["url"].startswith("https://r2.example.test/")
    assert signed["expires_seconds"] <= 600
    assert signed["size_bytes"] == 64 * 1024**2
    assert store.client.signed_parts[-1]["ContentLength"] == 64 * 1024**2
    assert uploads.status(channel, uuid.UUID(started["source_id"]))["parts"] == []

    store.client.upload_parts = [
        {"PartNumber": index, "Size": 64 * 1024**2, "ETag": f'"part-{index}"'}
        for index in range(1, 49)
    ]
    resumed = uploads.status(channel, uuid.UUID(started["source_id"]))
    assert len(resumed["parts"]) == 48
    assert resumed["parts"][0]["size"] == 64 * 1024**2
    with pytest.raises(ValueError, match="not found"):
        uploads.status(other, uuid.UUID(started["source_id"]))
    with pytest.raises(ValueError, match="outside"):
        uploads.sign_part(channel, uuid.UUID(started["source_id"]), 49)


def test_identity_mismatch_and_permission_rejection(upload_db):
    channel, _, store = upload_db
    begin(channel)
    with pytest.raises(ValueError, match="identity"):
        begin(channel, size_bytes=2 * 1024**3)
    with pytest.raises(ValueError, match="permission"):
        begin(channel, identity="not-authorized", permitted_use=False)
    with pytest.raises(ValueError, match="4 GiB"):
        begin(channel, identity="too-large", size_bytes=5 * 1024**3)
    assert len(store.client.uploads) == 1


def test_finish_promotes_only_verified_canonical_clip_and_recovers_replay(upload_db, monkeypatch):
    channel, _, store = upload_db
    started = begin(channel, size_bytes=2 * 64 * 1024**2)
    source_id = uuid.UUID(started["source_id"])
    store.client.upload_parts = [
        {"PartNumber": index, "Size": 64 * 1024**2, "ETag": f'"p{index}"'}
        for index in range(1, 3)
    ]
    hashed = []
    monkeypatch.setattr(
        uploads, "_hash_object",
        lambda object_store, key, expected: hashed.append((key, expected)) or "a" * 64,
    )
    monkeypatch.setattr(
        uploads, "_probe_object",
        lambda object_store, key: (
            {"format": {"duration": "125"}, "streams": [
                {"codec_type": "video", "width": 3840, "height": 2160}
            ]},
            125.0, 3840, 2160,
        ),
    )
    receipt = uploads.finish(channel, source_id, actor="test")
    assert receipt["clip_id"]
    assert receipt["state"] == "ready"
    assert receipt["width"] == 3840 and receipt["height"] == 2160
    assert receipt["duration_seconds"] == 125
    assert receipt["sha256"] == "a" * 64
    assert hashed[0][1] == 128 * 1024**2
    assert len(store.client.completed) == 1
    assert store.client.copied[0]["Key"].startswith("raw/aa/")
    assert store.deleted
    assert uploads.finish(channel, source_id, actor="test") == receipt
    with db.session_scope() as session:
        source = session.get(SourceItem, source_id)
        assert source.clip_id == uuid.UUID(receipt["clip_id"])
        assert session.get(Clip, source.clip_id).sha256 == "a" * 64


def test_part_receipts_reject_skipped_or_wrong_size(upload_db):
    channel, _, _ = upload_db
    started = begin(channel, size_bytes=65 * 1024**2)
    with db.session_scope() as session:
        meta = session.get(SourceItem, uuid.UUID(started["source_id"])).source_metadata
    with pytest.raises(ValueError, match="Not all"):
        uploads._validate_completed_parts([], meta)
    with pytest.raises(ValueError, match="unexpected sizes"):
        uploads._validate_completed_parts([
            {"part_number": 1, "size": 64 * 1024**2, "etag": '"one"'},
            {"part_number": 2, "size": 2 * 1024**2, "etag": '"two"'},
        ], meta)
    valid = uploads._validate_completed_parts([
        {"part_number": 1, "size": 64 * 1024**2, "etag": '"one"'},
        {"part_number": 2, "size": 1024**2, "etag": '"two"'},
    ], meta)
    assert [item["PartNumber"] for item in valid] == [1, 2]


def test_fence_denial_before_upload_creation_or_signed_url(upload_db, monkeypatch):
    channel, _, store = upload_db

    def deny(*_a, **_k):
        raise RuntimeError("stale leader")

    monkeypatch.setattr(uploads, "assert_mutation_authority", deny)
    with pytest.raises(RuntimeError, match="stale"):
        begin(channel)
    assert not store.client.uploads
    monkeypatch.setattr(uploads, "assert_mutation_authority", lambda *a, **k: None)
    started = begin(channel, identity="second")
    monkeypatch.setattr(uploads, "assert_mutation_authority", deny)
    with pytest.raises(RuntimeError, match="stale"):
        uploads.sign_part(channel, uuid.UUID(started["source_id"]), 1)


def test_named_principal_channel_and_mutation_scope():
    channel = uuid.uuid4()
    request = Request({
        "type": "http", "method": "POST", "path": "/v1/test",
        "headers": [], "query_string": b"",
        "state": {
            "control_actor": "test", "control_principal_name": "limited",
            "control_scopes": {"ai:read"},
            "control_channel_profile_ids": {str(channel)},
        },
    })
    with pytest.raises(HTTPException) as denied:
        _auth(request, channel, write=True)
    assert denied.value.status_code == 403
    _auth(request, channel, write=False)
    with pytest.raises(HTTPException) as wrong_channel:
        _auth(request, uuid.uuid4(), write=False)
    assert wrong_channel.value.status_code == 403


def test_aborted_upload_may_be_restarted_with_same_source_identity(upload_db):
    channel, _, store = upload_db
    initial = begin(channel, identity="restart-me")
    cancelled = uploads.cancel(channel, uuid.UUID(initial["source_id"]))
    assert cancelled["state"] == "cancelled"
    assert len(store.client.aborted) == 1
    restarted = begin(channel, identity="restart-me")
    assert restarted["source_id"] == initial["source_id"]
    assert restarted["state"] == "uploading"
    assert len(store.client.uploads) == 2


def test_size_verification_rejects_truncation_without_promoting_media(upload_db):
    _, _, store = upload_db
    key = "upload-staging/test-truncated"
    store.client.items[key] = b"short"
    with pytest.raises(ValueError, match="size"):
        uploads._hash_object(store, key, 8)
    assert key in store.client.items
    assert not store.client.copied


def test_last_part_is_exactly_signed_and_abandoned_verification_can_be_discarded(upload_db):
    channel, _, store = upload_db
    started = begin(channel, identity="unequal-last", size_bytes=65 * 1024**2)
    sid = uuid.UUID(started["source_id"])
    last = uploads.sign_part(channel, sid, 2)
    assert last["size_bytes"] == 1024**2
    assert store.client.signed_parts[-1]["ContentLength"] == 1024**2
    with db.session_scope() as session:
        source = session.get(SourceItem, sid)
        meta = dict(source.source_metadata)
        meta["state"] = "verifying"
        source.source_metadata = meta
    with pytest.raises(ValueError, match="no longer"):
        uploads.sign_part(channel, sid, 1)
    abandoned = uploads.cancel(channel, sid)
    assert abandoned["state"] == "cancelled"
    assert len(store.client.aborted) == 1


def test_real_http_scoping_and_input_validation(upload_db, monkeypatch):
    channel, other, store = upload_db
    settings = Settings(
        _env_file=None,
        env="test",
        control_api_token=None,
        control_principals=[
            {
                "name": "editor", "token": "large-media-test-editor-token-001",
                "scopes": ["production:create", "ai:read"],
                "channel_profile_ids": [str(channel)],
            },
            {
                "name": "reader", "token": "large-media-test-reader-token-002",
                "scopes": ["ai:read"],
                "channel_profile_ids": [str(channel)],
            },
        ],
    )
    monkeypatch.setattr("katcha.api.control_auth.get_settings", lambda: settings)
    base = f"/v1/channels/{channel}/editorial-projects/source-uploads/multipart"
    payload = {
        "filename": "trailer.mp4", "title": "4k fixture",
        "size_bytes": 3 * 1024**3, "content_type": "video/mp4",
        "idempotency_key": "web-upload-fixture", "permitted_use": True,
    }
    client = TestClient(
        app, headers={"Authorization": "Bearer large-media-test-editor-token-001"}
    )
    try:
        initial = client.post(base, json=payload)
        assert initial.status_code == 201, initial.text
        sid = initial.json()["source_id"]
        assert initial.json()["part_count"] == 48
        assert client.get(f"{base}/{sid}").status_code == 200
        part = client.post(f"{base}/{sid}/parts", json={"part_number": 1})
        assert part.status_code == 200, part.text
        assert part.json()["size_bytes"] == 64 * 1024**2
        assert client.post(base, json=payload | {"size_bytes": 0}).status_code == 422
        assert client.post(base, json=payload | {"permitted_use": False}).status_code == 422
        assert client.post(
            f"/v1/channels/{other}/editorial-projects/source-uploads/multipart",
            json=payload,
        ).status_code == 403
        reader_headers = {"Authorization": "Bearer large-media-test-reader-token-002"}
        assert client.get(f"{base}/{sid}", headers=reader_headers).status_code == 200
        assert client.post(
            f"{base}/{sid}/parts", json={"part_number": 1},
            headers=reader_headers,
        ).status_code == 403
    finally:
        client.close()
    assert len(store.client.uploads) == 1


def test_cross_worker_verification_is_denied_when_postgres_lock_is_held(monkeypatch):
    from contextlib import contextmanager

    class LockedSession:
        def get_bind(self):
            return SimpleNamespace(dialect=SimpleNamespace(name="postgresql"))

        def scalar(self, statement, params):
            assert "pg_try_advisory_xact_lock" in str(statement)
            assert isinstance(params["key"], int)
            return False

    @contextmanager
    def locked_connection():
        yield LockedSession()

    monkeypatch.setattr(uploads, "session_scope", locked_connection)
    with pytest.raises(ValueError, match="verification is already running"):
        uploads.finish(uuid.uuid4(), uuid.uuid4(), actor="operator")
    with pytest.raises(ValueError, match="verification is already running"):
        uploads.cancel(uuid.uuid4(), uuid.uuid4())


def test_failover_between_part_listing_and_commit_blocks_external_mutation(
    upload_db, monkeypatch
):
    channel, _, store = upload_db
    started = begin(channel, identity="fence-complete", size_bytes=64 * 1024**2)
    source_id = uuid.UUID(started["source_id"])
    store.client.upload_parts = [
        {"PartNumber": 1, "Size": 64 * 1024**2, "ETag": '"verified"'}
    ]

    def revoke(action, **kwargs):
        if action == "storage.multipart.complete_parts":
            raise RuntimeError("leadership epoch changed")

    monkeypatch.setattr(uploads, "assert_mutation_authority", revoke)
    with pytest.raises(RuntimeError, match="leadership"):
        uploads.finish(channel, source_id, actor="test")
    assert not store.client.completed
    assert not store.client.copied
    assert uploads.status(channel, source_id)["state"] == "uploading"


def test_failover_after_hash_blocks_canonical_copy_and_registration(
    upload_db, monkeypatch
):
    channel, _, store = upload_db
    started = begin(channel, identity="fence-copy", size_bytes=64 * 1024**2)
    source_id = uuid.UUID(started["source_id"])
    with db.session_scope() as session:
        row = session.get(SourceItem, source_id)
        stage = row.source_metadata["staging_key"]
    store.client.items[stage] = b"media"
    monkeypatch.setattr(
        uploads, "_hash_object", lambda *_args: "c" * 64
    )
    monkeypatch.setattr(
        uploads, "_probe_object",
        lambda *_args: (
            {"format": {"duration": "3"}, "streams": [
                {"codec_type": "video", "width": 3840, "height": 2160}
            ]},
            3.0, 3840, 2160,
        ),
    )

    def revoke(action, **kwargs):
        if action == "storage.multipart.copy_canonical":
            raise RuntimeError("leadership epoch changed")

    monkeypatch.setattr(uploads, "assert_mutation_authority", revoke)
    with pytest.raises(RuntimeError, match="leadership"):
        uploads.finish(channel, source_id, actor="test")
    assert not store.client.copied
    with db.session_scope() as session:
        assert session.scalar(
            __import__("sqlalchemy").select(Clip).where(Clip.sha256 == "c" * 64)
        ) is None
        assert session.get(SourceItem, source_id).clip_id is None
