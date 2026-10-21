"""Direct object-storage ingest integration without live provider calls."""

from __future__ import annotations

import hashlib
import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from test_command_goals import saved as saved_fixture

from katcha import db
from katcha.domain import SourceStatus
from katcha.integrations.direct_media import DirectMediaStreamResult
from katcha.models import Clip, DomainEvent, SourceItem
from katcha.orchestration import activities

saved = saved_fixture


class StageStore:
    def __init__(self):
        self.objects = {}
        self.deleted = []
        self.moves = []

    def ensure_bucket(self):
        pass

    def raw_key(self, sha, ext):
        return f"raw/{sha[:2]}/{sha}.{ext}"

    def exists(self, key):
        return key in self.objects

    def move(self, source, destination):
        self.moves.append((source, destination))
        self.objects[destination] = self.objects.pop(source)

    def delete(self, key):
        self.deleted.append(key)
        self.objects.pop(key, None)


class StageWriter:
    def __init__(self, store, key, content_type):
        self.store = store
        self.key = key
        self.data = bytearray()
        self.aborted = False

    def write(self, data):
        self.data.extend(data)

    def finish(self):
        self.store.objects[self.key] = bytes(self.data)
        return len(self.data)

    def abort(self):
        self.aborted = True


@pytest.fixture
def transfer_stubs(monkeypatch):
    store = StageStore()
    media = b"fixture movie bytes " * 128
    attempts = []

    def fake_stream(url, consume):
        attempts.append(url)
        consume(media[:31])
        consume(media[31:])
        return DirectMediaStreamResult(
            sha256=hashlib.sha256(media).hexdigest(),
            size_bytes=len(media),
            extension="mp4",
            content_type="video/mp4",
            final_host="press.example.org",
            redirects=0,
        )

    def remote_verify(store, key, expected, size):
        assert len(store.objects[key]) == size
        assert hashlib.sha256(store.objects[key]).hexdigest() == expected

    monkeypatch.setattr(activities, "ObjectStore", lambda: store)
    monkeypatch.setattr(activities, "MultipartObjectWriter", StageWriter)
    monkeypatch.setattr(activities, "stream_direct_media", fake_stream)
    monkeypatch.setattr(activities, "verify_stored_sha256", remote_verify)
    monkeypatch.setattr(
        activities,
        "inspect_stored_media",
        lambda store, key, size: {
            "format": {"duration": "3.0", "size": str(size)},
            "streams": [{"codec_type": "video", "width": 3840, "height": 2160}],
        },
    )
    monkeypatch.setattr(activities, "pg_insert", sqlite_insert)
    return store, attempts, media


def add_source(url):
    with db.session_scope() as session:
        source = SourceItem(
            id=uuid.uuid4(),
            source_url=url,
            canonical_url=url,
            platform="generic",
            status=SourceStatus.REGISTERED.value,
            title="Original trailer title",
            creator="Publisher",
            source_metadata={"rights_lane": "yellow", "channel_profile_id": "channel-1"},
        )
        session.add(source)
        return source.id


def test_streamed_source_becomes_real_clip_and_preserves_provenance(
    saved, transfer_stubs
):
    store, attempts, media = transfer_stubs
    source_id = add_source("https://press.example.org/video.mp4")
    with db.session_scope() as session:
        source = session.get(SourceItem, source_id)
        source.source_metadata = {
            **source.source_metadata,
            "ingest_recovery": {
                "code": "ingest_temporal_visibility_unavailable",
                "workflow_status": "UNVERIFIED",
            },
        }
    result = activities._ingest_source(str(source_id))
    assert result["deduped"] is False
    assert attempts == ["https://press.example.org/video.mp4"]
    assert len(store.objects) == 1
    with db.session_scope() as session:
        clip = session.get(Clip, uuid.UUID(result["clip_id"]))
        source = session.get(SourceItem, source_id)
        assert clip.storage_key in store.objects
        assert store.objects[clip.storage_key] == media
        assert clip.size_bytes == len(media)
        assert clip.sha256 == hashlib.sha256(media).hexdigest()
        assert clip.width == 3840 and clip.height == 2160
        assert source.clip_id == clip.id
        assert source.status == SourceStatus.READY.value
        assert source.title == "Original trailer title"
        assert source.creator == "Publisher"
        assert source.source_metadata["rights_lane"] == "yellow"
        assert source.source_metadata["channel_profile_id"] == "channel-1"
        assert source.source_metadata["acquisition_transport"] == "pinned_https_r2_multipart"
        claim = source.source_metadata["direct_promotion_proof"]
        assert claim["sha256"] == clip.sha256
        assert claim["source_id"] == str(source_id)
        assert claim["raw_key"] == clip.storage_key
        assert claim["stage_key"].startswith(f"staging/direct/{source_id}/")
        events = list(session.scalars(
            select(DomainEvent).where(DomainEvent.aggregate_id == str(source_id))
        ))
        assert "source.direct_media_stage_verified" in [event.event_type for event in events]
        assert "ingest_recovery" not in source.source_metadata
    assert not any(key.startswith("staging/") for key in store.objects)

    # The same source's workflow retry must return the existing clip, no new bytes.
    again = activities._ingest_source(str(source_id))
    assert again["clip_id"] == result["clip_id"]
    assert again["deduped"] is True
    assert len(attempts) == 1


def test_duplicate_direct_source_reuses_clip_without_duplicate_storage(
    saved, transfer_stubs
):
    store, attempts, _ = transfer_stubs
    first = activities._ingest_source(
        str(add_source("https://press.example.org/a.mp4"))
    )
    second = activities._ingest_source(
        str(add_source("https://press.example.org/b.mp4"))
    )
    assert second["clip_id"] == first["clip_id"]
    assert second["deduped"] is True
    assert len(attempts) == 2
    assert len(store.objects) == 1
    assert store.deleted  # duplicate's staging object was discarded


def test_failed_remote_sha_never_registers_clip_and_cleans_stage(
    saved, transfer_stubs, monkeypatch
):
    store, _, _ = transfer_stubs
    source_id = add_source("https://press.example.org/c.mp4")

    def corrupted(*args):
        raise ValueError("Stored media SHA-256 differs from the acquired source")

    monkeypatch.setattr(activities, "verify_stored_sha256", corrupted)
    with pytest.raises(Exception, match="SHA-256"):
        activities._ingest_source(str(source_id))
    with db.session_scope() as session:
        assert session.scalar(select(Clip)) is None
        assert session.get(SourceItem, source_id).clip_id is None
    assert not store.objects


def test_invalid_remote_video_never_registers_clip(
    saved, transfer_stubs, monkeypatch
):
    store, _, _ = transfer_stubs
    source_id = add_source("https://press.example.org/d.mp4")
    monkeypatch.setattr(
        activities, "inspect_stored_media",
        lambda *_: (_ for _ in ()).throw(ValueError("Stored media has no video")),
    )
    with pytest.raises(Exception, match="no video"):
        activities._ingest_source(str(source_id))
    with db.session_scope() as session:
        assert session.scalar(select(Clip)) is None
    assert not store.objects
