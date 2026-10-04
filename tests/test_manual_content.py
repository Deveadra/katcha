from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from katcha import db
from katcha.api.main import app
from katcha.content_models import ContentItem
from katcha.intelligence_models import ChannelProfile
from katcha.models import Clip, SourceItem
from katcha.publishing_models import Publication, YouTubeConnection
from katcha.services import content, productions, publication_plans
from katcha.services.publications import register_publication, release_publication_for_upload


class MemoryStore:
    objects: dict[str, bytes] = {}

    def ensure_bucket(self):
        pass

    def put_bytes(self, data, key, mime):
        self.objects[key] = data

    def exists(self, key):
        return key in self.objects

    def get_bytes(self, key):
        return self.objects[key]

    def put_file(self, path, key):
        self.objects[key] = path.read_bytes()

    def delete(self, key):
        self.objects.pop(key, None)

    def raw_key(self, digest, extension):
        return f"raw/{digest}.{extension}"


@pytest.fixture()
def workspace(monkeypatch, tmp_path):
    engine = create_engine(
        "sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    db.load_model_metadata()
    db.Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(db, "SessionLocal", factory)
    monkeypatch.setattr(content, "ObjectStore", MemoryStore)
    monkeypatch.setattr(productions, "ObjectStore", MemoryStore)
    monkeypatch.setattr(content, "get_settings", lambda: SimpleNamespace(work_dir=tmp_path))
    monkeypatch.setattr(
        content,
        "ffprobe",
        lambda path: {
            "streams": [{"codec_type": "video", "width": 1920, "height": 1080}],
            "format": {"duration": "10"},
        },
    )
    MemoryStore.objects = {}
    with db.session_scope() as session:
        conn = YouTubeConnection(
            channel_id=f"fixture-{uuid.uuid4()}",
            channel_title="Fixture",
            status="active",
            scopes=[],
            encrypted_access_token="fixture",
            encrypted_refresh_token="fixture",
            token_expires_at=datetime.now(UTC) + timedelta(hours=1),
        )
        session.add(conn)
        session.flush()
        profile = ChannelProfile(
            youtube_connection_id=conn.id,
            status="active",
            timezone="America/Chicago",
            profile_metadata={},
        )
        session.add(profile)
        session.flush()
        channel_id = profile.id
    yield channel_id, conn.id, factory
    engine.dispose()


def intake(channel_id, *, key="test-input", data=b"video", **overrides):
    return content.create_content(
        channel_id,
        request_key=key,
        input_kind="file",
        title="Fixture",
        filename="fixture.webm",
        size_bytes=len(data),
        details={"fingerprint": "fixture", "rights_confirmed": True},
        **overrides,
    )


def test_chunk_replay_resume_and_identity_conflict(workspace):
    channel, _, _ = workspace
    row = intake(channel, data=b"video-bytes")
    assert content.accept_chunk(channel, row.id, 0, b"video")["upload_offset"] == 5
    assert content.accept_chunk(channel, row.id, 0, b"video")["replayed"]
    assert intake(channel, data=b"video-bytes").id == row.id
    with pytest.raises(ValueError, match="differs"):
        content.accept_chunk(channel, row.id, 0, b"other")
    with pytest.raises(ValueError, match="offset"):
        content.accept_chunk(channel, row.id, 6, b"bytes")
    with pytest.raises(ValueError, match="different input"):
        intake(channel, data=b"different")
    with pytest.raises(ValueError, match="not finished"):
        content.finish_upload(channel, row.id)
    content.accept_chunk(channel, row.id, 5, b"-bytes")
    first = content.finish_upload(channel, row.id)
    content.cleanup_completed_upload(channel, row.id)
    assert not any(key.startswith("intake/") for key in MemoryStore.objects)
    assert content.finish_upload(channel, row.id)["clip_id"] == first["clip_id"]
    with db.session_scope() as session:
        assert session.scalar(select(Clip)).sha256 == hashlib.sha256(b"video-bytes").hexdigest()
        assert session.scalar(select(SourceItem)).platform == "local_file"
        assert session.scalar(select(ContentItem)).upload_offset == 11


def test_upload_validates_video_and_deduplicates_bytes(workspace, monkeypatch):
    channel, _, _ = workspace
    row = intake(channel)
    content.accept_chunk(channel, row.id, 0, b"video")
    monkeypatch.setattr(content, "ffprobe", lambda _: {"streams": [], "format": {}})
    with pytest.raises(ValueError, match="playable"):
        content.finish_upload(channel, row.id)
    monkeypatch.setattr(
        content,
        "ffprobe",
        lambda _: {
            "streams": [{"codec_type": "video", "width": 1280, "height": 720}],
            "format": {"duration": "10"},
        },
    )
    one = content.finish_upload(channel, row.id)
    two = intake(channel, key="another-receipt")
    content.accept_chunk(channel, two.id, 0, b"video")
    assert content.finish_upload(channel, two.id)["clip_id"] == one["clip_id"]
    with db.session_scope() as session:
        assert len(list(session.scalars(select(Clip)))) == 1
        assert len(list(session.scalars(select(ContentItem)))) == 2


def make_publication(workspace):
    channel, connection, _ = workspace
    row = intake(channel)
    content.accept_chunk(channel, row.id, 0, b"video")
    clip = uuid.UUID(content.finish_upload(channel, row.id)["clip_id"])
    production = productions.register_source_passthrough_production(
        clip, channel_profile_id=channel
    )
    return register_publication(
        production.id, youtube_connection_id=connection, title="Fixture", hold_for_packaging=True
    ), row


def save(row, **overrides):
    values = dict(
        title="Manual title",
        description="Manual description",
        tags=["news"],
        publish_mode="private",
        publish_at=None,
        channel_local_time=None,
        notify_subscribers=False,
        made_for_kids=False,
        contains_synthetic_media=False,
        late_policy="hold",
        expected_version=0,
        actor="fixture",
    )
    values.update(overrides)
    return publication_plans.save_publication_draft(row.id, **values)


def test_manual_no_ai_path_metadata_timezone_and_stale_start(workspace):
    row, receipt = make_publication(workspace)
    saved = save(row, publish_mode="scheduled", channel_local_time="2030-07-01T09:30")
    assert saved.publish_at.replace(tzinfo=UTC) == datetime(2030, 7, 1, 14, 30, tzinfo=UTC)
    assert saved.title == "Manual title"
    assert saved.raw_status["manual_plan_version"] == 1
    assert saved.raw_status["late_policy"] == "hold"
    with pytest.raises(ValueError, match="Another tab"):
        save(row)
    with pytest.raises(ValueError, match="Another tab"):
        release_publication_for_upload(row.id, expected_version=0)
    assert release_publication_for_upload(row.id, expected_version=1).stage == "queued"
    page = content.list_content(workspace[0])
    assert page["items"][0]["publications"][0]["id"] == row.id
    assert page["items"][0]["productions"][0]["kind"] == "source_passthrough"
    assert page["items"][0]["id"] == receipt.id


@pytest.mark.parametrize("value", ["2030-03-10T02:30", "2030-11-03T01:30"])
def test_dst_missing_and_ambiguous_times_are_rejected(value):
    with pytest.raises(ValueError, match="missing or ambiguous"):
        publication_plans.channel_time_to_utc(value, "America/Chicago")


def test_intake_api_receipt_resume_and_rights_required(workspace):
    channel, _, _ = workspace
    with TestClient(app) as client:
        url = f"/v1/channels/{channel}/content"
        body = {
            "request_key": "api-fixture",
            "input_kind": "file",
            "title": "Video",
            "filename": "fixture.mp4",
            "size_bytes": 5,
            "fingerprint": "fixture",
        }
        assert client.post(url, json=body).status_code == 422
        body["rights_confirmed"] = True
        response = client.post(url, json=body)
        assert response.status_code == 201, response.text
        row = response.json()
        assert row["upload_offset"] == 0
        assert client.put(f"{url}/{row['id']}/chunks?offset=0", content=b"video").status_code == 200
        replay = client.post(url, json=body).json()
        assert replay["id"] == row["id"]
        assert replay["upload_offset"] == 5
        assert replay["chunks"][0]["sha256"] == hashlib.sha256(b"video").hexdigest()
        assert client.get(f"/v1/channels/{uuid.uuid4()}/content/{row['id']}").status_code == 404


def test_missed_schedule_remains_private_and_thumbnail_precedes_release(workspace, monkeypatch):
    from katcha.orchestration import publishing_activities

    row, _ = make_publication(workspace)
    with db.session_scope() as session:
        stored = session.get(Publication, row.id)
        stored.youtube_video_id = "fixture-video"
        stored.publish_at = datetime.now(UTC) - timedelta(hours=1)
        stored.privacy_status = "public"
        stored.raw_status = {
            "late_policy": "hold",
            "manual_thumbnail": {"key": "thumb", "content_type": "image/png"},
        }
    MemoryStore.objects["thumb"] = b"fixture-thumbnail"
    calls = []

    class Client:
        def __init__(self, *args, **kwargs):
            pass

        def set_thumbnail(self, video_id, *, data, mime_type):
            calls.append(("thumbnail", mime_type))

        def set_privacy(self, video_id, **kwargs):
            calls.append(("privacy", kwargs["privacy_status"]))
            return {"status": {"privacyStatus": kwargs["privacy_status"]}}

    monkeypatch.setattr(publishing_activities, "YouTubeClient", Client)
    monkeypatch.setattr(publishing_activities, "ObjectStore", MemoryStore)
    result = publishing_activities.finalize_publication_activity(str(row.id))
    assert result["status"] == "private"
    assert calls == [("thumbnail", "image/png"), ("privacy", "private")]
    with db.session_scope() as session:
        assert session.get(Publication, row.id).stage == "schedule_missed"


def test_live_release_uncertainty_requires_reconciliation(workspace, monkeypatch):
    from katcha.integrations.youtube import client as youtube

    row, _ = make_publication(workspace)
    with db.session_scope() as session:
        stored = session.get(Publication, row.id)
        stored.youtube_video_id = "fixture-video"
        stored.status = stored.stage = "private"
    observed = {"privacyStatus": "private"}

    class Client:
        def __init__(self, *args, **kwargs):
            pass

        def video_resource(self, video_id):
            return {"status": dict(observed)}

        def set_privacy(self, video_id, **kwargs):
            observed["privacyStatus"] = kwargs["privacy_status"]
            raise TimeoutError("Fixture response lost after provider mutation")

    monkeypatch.setattr(youtube, "YouTubeClient", Client)
    values = dict(
        publish_mode="asap",
        publish_at=None,
        channel_local_time=None,
        expected_version=0,
        actor="fixture",
    )
    with pytest.raises(ValueError, match="may have reached"):
        publication_plans.change_live_release(row.id, **values)
    with db.session_scope() as session:
        stored = session.get(Publication, row.id)
        assert stored.status == "private"
        assert stored.raw_status["release_confirmation"]["state"] == "uncertain"
        assert stored.raw_status["manual_plan_version"] == 1
    values["expected_version"] = 1
    with pytest.raises(ValueError, match="Reconcile"):
        publication_plans.change_live_release(row.id, **values)
    confirmed = publication_plans.reconcile_live_release(row.id, actor="fixture")
    assert confirmed.status == "published"
    assert confirmed.raw_status["manual_plan_version"] == 2


def test_manual_thumbnail_verification_and_lock(workspace, monkeypatch):
    from io import BytesIO

    from PIL import Image

    from katcha.integrations import storage

    row, _ = make_publication(workspace)
    monkeypatch.setattr(storage, "ObjectStore", MemoryStore)
    with pytest.raises(ValueError, match="valid JPEG"):
        publication_plans.save_thumbnail(
            row.id, b"not-an-image", expected_version=0, actor="fixture"
        )
    buffer = BytesIO()
    Image.new("RGB", (1280, 720)).save(buffer, format="PNG")
    saved = publication_plans.save_thumbnail(
        row.id, buffer.getvalue(), expected_version=0, actor="fixture"
    )
    assert saved.raw_status["manual_thumbnail"]["content_type"] == "image/png"
    release_publication_for_upload(row.id, expected_version=1)
    with pytest.raises(ValueError, match="locked"):
        publication_plans.save_thumbnail(
            row.id, buffer.getvalue(), expected_version=1, actor="fixture"
        )


def test_recurring_source_checks_use_existing_dispatcher_and_preserve_pause(workspace):
    from katcha.services.ingestion_sources import upsert_ingestion_source
    from katcha.services.research import prepare_research_jobs
    from katcha.services.source_polling import configure_polling

    channel, _, _ = workspace
    args = dict(
        source_key="fixture-feed",
        name="Feed",
        adapter_key="rss_atom",
        adapter_version="v1",
        platform="web",
        channel_profile_id=channel,
        query_template={"url": "https://example.test/feed"},
    )
    source = upsert_ingestion_source(**args)
    configure_polling(source.id, enabled=False, interval_minutes=60, actor="fixture")
    assert prepare_research_jobs() == []
    configure_polling(source.id, enabled=True, interval_minutes=60, actor="fixture")
    now = datetime.now(UTC)
    one = prepare_research_jobs(now)
    assert len(one) == 1
    assert prepare_research_jobs(now)[0]["workflow_id"] == one[0]["workflow_id"]
    configure_polling(source.id, enabled=False, interval_minutes=60, actor="fixture")
    # A generic source edit with a stale metadata snapshot cannot reactivate recurrence.
    upsert_ingestion_source(**args, source_metadata={"automatic_research": True})
    assert prepare_research_jobs(now + timedelta(hours=2)) == []


def test_new_routes_enforce_scoped_channel_and_read_only_token(workspace, monkeypatch):

    from katcha.api import control_auth
    from katcha.config import Settings

    channel, _, _ = workspace
    settings = Settings(
        control_principals=[
            {
                "name": "fixture-reader",
                "token": "reader-token-12345",
                "scopes": ["channels:read"],
                "channel_profile_ids": [str(channel)],
            }
        ]
    )
    monkeypatch.setattr(control_auth, "get_settings", lambda: settings)
    with TestClient(app) as client:
        headers = {"Authorization": "Bearer reader-token-12345"}
        assert client.get(f"/v1/channels/{channel}/content", headers=headers).status_code == 200
        assert (
            client.get(f"/v1/channels/{uuid.uuid4()}/content", headers=headers).status_code == 403
        )
        assert (
            client.post(f"/v1/channels/{channel}/content", headers=headers, json={}).status_code
            == 403
        )


def test_scoped_start_preserves_queue_on_uncertain_dispatch(workspace, monkeypatch):
    from katcha.api import control_auth, main
    from katcha.config import Settings

    row, _ = make_publication(workspace)
    save(row)
    channel, _, _ = workspace
    settings = Settings(
        control_principals=[
            {
                "name": "fixture-producer",
                "token": "producer-token-12345",
                "scopes": ["channels:read", "production:create"],
                "channel_profile_ids": [str(channel)],
            }
        ]
    )
    monkeypatch.setattr(control_auth, "get_settings", lambda: settings)
    monkeypatch.setattr(main, "_require_youtube_execution", lambda: None)
    calls = []

    async def dispatch(publication_id, workflow_id):
        calls.append(workflow_id)
        raise TimeoutError("Fixture dispatch response lost")

    monkeypatch.setattr(main, "start_publication_workflow", dispatch)
    with TestClient(app) as client:
        headers = {"Authorization": "Bearer producer-token-12345"}
        url = f"/v1/publications/{row.id}/start"
        for _ in range(2):
            response = client.post(
                url, headers=headers, json={"expected_version": 1, "actor": "spoofed"}
            )
            assert response.status_code == 202, response.text
            assert response.json()["stage"] == "queued"
            assert "could not be confirmed" in response.json()["error"]
        assert len(set(calls)) == 1
        assert client.post(url, headers=headers, json={"expected_version": 0}).status_code == 409
    with db.session_scope() as session:
        stored = session.get(Publication, row.id)
        assert (
            stored.raw_status["metadata_hold_released_by"] == "control-principal:fixture-producer"
        )


def test_url_receipt_scopes_shared_source_into_clips_without_mutating_source(workspace):
    from katcha.services.clip_lifecycle import channel_ids_for_clip, clip_ids_for_channel

    channel, _, _ = workspace
    with db.session_scope() as session:
        clip = Clip(
            sha256="c" * 64, storage_key="raw/fixture.mp4", extension="mp4", status="ingested"
        )
        session.add(clip)
        session.flush()
        source = SourceItem(
            source_url="https://example.test/video",
            canonical_url="https://example.test/video",
            platform="web",
            status="ready",
            clip_id=clip.id,
            source_metadata={},
        )
        session.add(source)
        session.flush()
        clip_id, source_id = clip.id, source.id
    content.create_content(
        channel,
        request_key="shared-url",
        input_kind="url",
        title="Working title",
        source_id=source_id,
        details={"fingerprint": "https://example.test/video"},
    )
    with db.session_scope() as session:
        assert clip_ids_for_channel(session, channel) == {clip_id}
        assert channel_ids_for_clip(session, clip_id) == {channel}
        assert session.get(SourceItem, source_id).source_metadata == {}
    assert content.list_content(channel)["items"][0]["title"] == "Working title"


def test_scoped_manual_prepare_and_review_are_ai_free_and_idempotent(workspace, monkeypatch):
    from katcha.api import control_auth
    from katcha.config import Settings

    channel, _, _ = workspace
    receipt = intake(channel)
    content.accept_chunk(channel, receipt.id, 0, b"video")
    content.finish_upload(channel, receipt.id)
    settings = Settings(
        control_principals=[
            {
                "name": "fixture-producer",
                "token": "producer-token-12345",
                "scopes": ["channels:read", "production:create"],
                "channel_profile_ids": [str(channel)],
            }
        ]
    )
    monkeypatch.setattr(control_auth, "get_settings", lambda: settings)
    with TestClient(app) as client:
        headers = {"Authorization": "Bearer producer-token-12345"}
        url = f"/v1/channels/{channel}/content/{receipt.id}"
        one = client.post(url + "/prepare", headers=headers, json={"mode": "preserve"})
        assert one.status_code == 200, one.text
        two = client.post(url + "/prepare", headers=headers, json={"mode": "preserve"})
        assert two.json()["id"] == one.json()["id"]
        publication = client.post(
            url + "/publication",
            headers=headers,
            json={"production_id": one.json()["id"], "title": "Manual video"},
        )
        assert publication.status_code == 200, publication.text
        assert publication.json()["stage"] == "metadata_hold"
        assert publication.json()["privacy_status"] == "private"
