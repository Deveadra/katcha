"""Synthetic evidence tests; no live research or factual-verification claim."""

import hashlib
import importlib.util
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from types import SimpleNamespace

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import create_engine, inspect, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from katcha import db
from katcha.api.main import app
from katcha.config import Settings
from katcha.editorial import source_uploads
from katcha.editorial.project_schemas import (
    CreateEditorialProject,
    EditorialDraft,
    SaveEditorialDraft,
)
from katcha.editorial_models import EditorialProject, EditorialRevision
from katcha.intelligence_models import ChannelProfile
from katcha.models import Clip, DomainEvent, SourceItem
from katcha.services import goal_tools
from katcha.services.clip_lifecycle import channel_ids_for_clip
from katcha.services.editorial_projects import EditorialConflict, create_project, save_draft


@pytest.fixture
def saved(monkeypatch):
    db.load_model_metadata()
    engine = create_engine(
        "sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    db.Base.metadata.create_all(engine)
    monkeypatch.setattr(db, "SessionLocal", sessionmaker(bind=engine, expire_on_commit=False))
    channel, other = uuid.uuid4(), uuid.uuid4()
    settings = Settings(
        _env_file=None,
        env="test",
        control_api_token=None,
        control_principals=[
            {
                "name": "editor",
                "token": "editorial-test-token-000001",
                "scopes": ["ai:read", "production:create"],
                "channel_profile_ids": [str(channel)],
            },
            {
                "name": "reader",
                "token": "editorial-reader-token-00001",
                "scopes": ["ai:read"],
                "channel_profile_ids": [str(channel)],
            },
        ],
    )
    monkeypatch.setattr("katcha.api.control_auth.get_settings", lambda: settings)
    with db.session_scope() as session:
        for value in (channel, other):
            session.add(
                ChannelProfile(
                    id=value,
                    youtube_connection_id=uuid.uuid4(),
                    status="active",
                    timezone="UTC",
                    profile_metadata={},
                )
            )
    client = TestClient(app, headers={"Authorization": "Bearer editorial-test-token-000001"})
    yield client, channel, other
    client.close()
    engine.dispose()


def root(channel):
    return f"/v1/channels/{channel}/editorial-projects"


def brief(key="create-1"):
    return {
        "idempotency_key": key,
        "brief": {
            "prompt": "Research a trailer and write a things-you-missed episode",
            "source_urls": ["https://www.youtube.com/watch?v=fixture"],
        },
    }


def managed_clip(channel, *, source_url="https://www.youtube.com/watch?v=fixture"):
    clip_id = uuid.uuid4()
    with db.session_scope() as session:
        session.add(
            Clip(
                id=clip_id,
                sha256=uuid.uuid4().hex * 2,
                storage_key=f"raw/{clip_id}.mp4",
                duration_seconds=90,
                width=1920,
                height=1080,
                status="scored",
            )
        )
        session.add(
            SourceItem(
                source_url=source_url,
                canonical_url=source_url,
                platform="youtube",
                status="ready",
                clip_id=clip_id,
                source_metadata={"channel_profile_id": str(channel)},
            )
        )
    return clip_id


def draft():
    return {
        "sources": [
            {
                "id": "source-1",
                "url": "https://example.com/interview",
                "title": "Interview",
                "category": "interview",
                "retrieved_at": "2026-10-02T12:00:00Z",
                "excerpt": "A synthetic source excerpt",
                "locator": "Paragraph 2",
            }
        ],
        "observations": [
            {
                "id": "observation-1",
                "source_url": "https://www.youtube.com/watch?v=fixture",
                "source_duration_seconds": 90,
                "start_seconds": 10,
                "end_seconds": 12,
                "observation": "An emblem is visible",
                "coverage": "manual",
            }
        ],
        "claims": [
            {
                "id": "claim-1",
                "text": "The emblem may connect to earlier material",
                "classification": "theory",
                "confidence": 0.4,
                "source_ids": ["source-1"],
                "observation_ids": ["observation-1"],
                "audience_value": "Explains the visual clue",
            }
        ],
        "script": [
            {
                "id": "beat-1",
                "role": "reveal",
                "narration": "This may be a connection.",
                "claim_ids": ["claim-1"],
                "uncertainty_disclosure": "Unconfirmed theory",
                "planned_duration_seconds": 8,
                "visual_intent": "Compare the emblem",
            }
        ],
    }


def test_api_roundtrip_replay_history_and_audit(saved):
    client, channel, _ = saved
    url = root(channel)
    first = client.post(url, json=brief())
    assert first.status_code == 201, first.text
    project = first.json()
    assert project["revision"] == 0
    assert project["status"] == "draft"
    assert "not connected" in project["limitation"]
    assert "render" not in project["capabilities"]
    assert client.post(url, json=brief()).json()["id"] == project["id"]
    revision_url = f"{url}/{project['id']}/revisions"
    body = {"expected_revision": 0, "idempotency_key": "save-1", "draft": draft()}
    saved_response = client.post(revision_url, json=body)
    assert saved_response.status_code == 201, saved_response.text
    replay = client.post(revision_url, json=body)
    assert replay.json() == saved_response.json()
    body.update(expected_revision=1, idempotency_key="save-2")
    body["draft"]["script"][0]["narration"] = "An updated theory."
    assert client.post(revision_url, json=body).status_code == 201
    history = client.get(revision_url).json()
    assert [row["revision"] for row in history] == [2, 1]
    assert history[1]["draft"]["script"][0]["narration"] == "This may be a connection."
    assert history[1]["actor"] == "control-principal:editor"
    assert client.get(f"{url}/{project['id']}").json()["revision"] == 2
    assert len(client.get(url).json()) == 1
    with db.session_scope() as session:
        assert len(list(session.scalars(select(EditorialProject)))) == 1
        assert len(list(session.scalars(select(EditorialRevision)))) == 2
        events = list(
            session.scalars(
                select(DomainEvent).where(DomainEvent.aggregate_type == "editorial_project")
            )
        )
        assert len(events) == 3


def test_storyboard_source_monitor_metadata_hides_storage_key(saved, monkeypatch):
    client, channel, _ = saved
    project_id = uuid.uuid4()
    run_id = uuid.uuid4()

    monkeypatch.setattr(
        "katcha.editorial.source_monitor.source_monitor",
        lambda *args: {
            "candidate_id": "candidate",
            "title": "Supporting interview",
            "source_url": "https://example.com/source",
            "clip_id": str(uuid.uuid4()),
            "sha256": "a" * 64,
            "duration_seconds": 10,
            "frame_count": 3,
            "sample_times": [0.25, 5.0, 9.75],
            "contact_sheet_key": "analysis/private/contact-sheet.jpg",
            "coverage": "sampled_frames",
            "limitation": "Sampled frames only.",
        },
    )

    response = client.get(
        f"{root(channel)}/{project_id}/runs/{run_id}/assets/candidate/source-monitor"
    )

    assert response.status_code == 200, response.text
    assert response.json()["candidate_id"] == "candidate"
    assert response.json()["sample_times"] == [0.25, 5.0, 9.75]
    assert "contact_sheet_key" not in response.json()


def test_source_upload_api_streams_to_temp_and_cleans_up(saved, monkeypatch):
    client, channel, _ = saved
    captured = {}

    def fake_import(
        channel_id,
        path,
        *,
        filename,
        content_type,
        title,
        permitted_use,
        idempotency_key,
        actor,
    ):
        captured.update(
            channel_id=channel_id,
            path=path,
            bytes=path.read_bytes(),
            filename=filename,
            content_type=content_type,
            title=title,
            permitted_use=permitted_use,
            idempotency_key=idempotency_key,
            actor=actor,
        )
        return source_uploads.ImportedSourceMedia(
            source_id=uuid.uuid4(),
            clip_id=uuid.uuid4(),
            source_url="https://upload.katcha.invalid/test",
            title=title or filename,
            filename=filename,
            sha256="a" * 64,
            size_bytes=9,
            duration_seconds=12.5,
            width=1920,
            height=1080,
            extension="mp4",
            deduplicated=False,
        )

    monkeypatch.setattr(source_uploads, "import_source_media", fake_import)
    response = client.post(
        f"{root(channel)}/source-uploads",
        params={
            "filename": "owned.mp4",
            "title": "Owned footage",
            "idempotency_key": "upload-api-1",
            "permitted_use": "true",
        },
        content=b"video-api",
        headers={"Content-Type": "video/mp4"},
    )

    assert response.status_code == 201, response.text
    assert captured["channel_id"] == channel
    assert captured["bytes"] == b"video-api"
    assert captured["filename"] == "owned.mp4"
    assert captured["content_type"] == "video/mp4"
    assert captured["title"] == "Owned footage"
    assert captured["permitted_use"] is True
    assert captured["idempotency_key"] == "upload-api-1"
    assert captured["actor"] == "control-principal:editor"
    assert not captured["path"].exists()


def test_operator_source_upload_is_managed_replay_safe_and_channel_scoped(
    saved, tmp_path, monkeypatch
):
    _, channel, _ = saved
    media = tmp_path / "owned-source.mp4"
    media.write_bytes(b"synthetic-video-transport")

    class FakeStore:
        objects = set()

        @staticmethod
        def raw_key(sha256, extension):
            return f"raw/{sha256}.{extension}"

        def exists(self, key):
            return key in self.objects

        def put_file(self, path, key, content_type=None):
            assert path == media
            assert content_type == "video/mp4"
            self.objects.add(key)

    monkeypatch.setattr(source_uploads, "ObjectStore", FakeStore)
    monkeypatch.setattr(
        source_uploads,
        "ffprobe",
        lambda path: {
            "format": {"duration": "42.5", "size": str(path.stat().st_size)},
            "streams": [{"codec_type": "video", "width": 1920, "height": 1080}],
        },
    )

    first = source_uploads.import_source_media(
        channel,
        media,
        filename="owned-source.mp4",
        content_type="video/mp4",
        title="Owned source",
        permitted_use=True,
        idempotency_key="upload-1",
        actor="test",
    )
    replay = source_uploads.import_source_media(
        channel,
        media,
        filename="owned-source.mp4",
        content_type="video/mp4",
        title="Owned source",
        permitted_use=True,
        idempotency_key="upload-1",
        actor="test",
    )

    assert first.source_id == replay.source_id
    assert first.clip_id == replay.clip_id
    assert replay.deduplicated is True
    assert first.source_url.startswith("https://upload.katcha.invalid/")
    body = brief("uploaded-source-project")
    body["brief"]["source_urls"] = [first.source_url]
    body["brief"]["source_clip_bindings"] = {first.source_url: str(first.clip_id)}
    response = saved[0].post(root(channel), json=body)
    assert response.status_code == 201, response.text
    assert response.json()["brief"]["source_clip_bindings"] == {
        first.source_url: str(first.clip_id)
    }
    with db.session_scope() as session:
        assert channel in channel_ids_for_clip(session, first.clip_id)
        sources = list(
            session.scalars(select(SourceItem).where(SourceItem.clip_id == first.clip_id))
        )
        assert len(sources) == 1


def test_operator_source_upload_rejects_reused_identity_for_different_media(
    saved, tmp_path, monkeypatch
):
    _, channel, _ = saved
    first_path = tmp_path / "first.mp4"
    second_path = tmp_path / "second.mp4"
    first_path.write_bytes(b"first-video")
    second_path.write_bytes(b"second-video")

    class FakeStore:
        objects = set()

        @staticmethod
        def raw_key(sha256, extension):
            return f"raw/{sha256}.{extension}"

        def exists(self, key):
            return key in self.objects

        def put_file(self, path, key, content_type=None):
            self.objects.add(key)

    monkeypatch.setattr(source_uploads, "ObjectStore", FakeStore)
    monkeypatch.setattr(
        source_uploads,
        "ffprobe",
        lambda path: {
            "format": {"duration": "10", "size": str(path.stat().st_size)},
            "streams": [{"codec_type": "video", "width": 1280, "height": 720}],
        },
    )
    source_uploads.import_source_media(
        channel,
        first_path,
        filename="first.mp4",
        content_type="video/mp4",
        title=None,
        permitted_use=True,
        idempotency_key="same-request",
        actor="test",
    )
    with pytest.raises(ValueError, match="already used for different source media"):
        source_uploads.import_source_media(
            channel,
            second_path,
            filename="second.mp4",
            content_type="video/mp4",
            title=None,
            permitted_use=True,
            idempotency_key="same-request",
            actor="test",
        )


def test_project_persists_provenance_checked_script_seed(saved):
    client, channel, _ = saved
    body = brief("script-seed")
    text = "# Draft\n\nThis might connect to earlier material."
    body["brief"]["script_seed"] = {
        "text": text,
        "origin": "operator_file",
        "content_sha256": hashlib.sha256(text.encode()).hexdigest(),
        "filename": "draft.md",
        "media_type": "text/markdown",
        "source_file_sha256": "a" * 64,
    }

    response = client.post(root(channel), json=body)
    assert response.status_code == 201, response.text
    seed = response.json()["brief"]["script_seed"]
    assert seed["origin"] == "operator_file"
    assert seed["filename"] == "draft.md"


def test_project_rejects_script_seed_with_mismatched_content_hash(saved):
    client, channel, _ = saved
    body = brief("bad-script-seed")
    body["brief"]["script_seed"] = {
        "text": "Operator draft",
        "origin": "operator_paste",
        "content_sha256": "0" * 64,
        "media_type": "text/plain",
    }

    response = client.post(root(channel), json=body)
    assert response.status_code == 422
    assert "content hash" in response.text


def test_project_persists_verified_managed_source_binding(saved):
    client, channel, _ = saved
    clip_id = managed_clip(channel)
    body = brief("managed-source")
    source_url = body["brief"]["source_urls"][0]
    body["brief"]["source_clip_bindings"] = {source_url: str(clip_id)}

    response = client.post(root(channel), json=body)
    assert response.status_code == 201, response.text
    project = response.json()
    assert project["brief"]["source_clip_bindings"] == {source_url: str(clip_id)}
    assert client.post(root(channel), json=body).json()["id"] == project["id"]


def test_project_rejects_managed_clip_without_matching_lineage(saved):
    client, channel, _ = saved
    clip_id = managed_clip(channel, source_url="https://www.youtube.com/watch?v=other")
    body = brief("wrong-lineage")
    source_url = body["brief"]["source_urls"][0]
    body["brief"]["source_clip_bindings"] = {source_url: str(clip_id)}

    response = client.post(root(channel), json=body)
    assert response.status_code == 409
    assert "does not match" in response.json()["detail"]


def test_project_rejects_managed_clip_from_another_channel(saved):
    client, channel, other = saved
    clip_id = managed_clip(other)
    body = brief("wrong-channel")
    source_url = body["brief"]["source_urls"][0]
    body["brief"]["source_clip_bindings"] = {source_url: str(clip_id)}

    response = client.post(root(channel), json=body)
    assert response.status_code == 409
    assert "not available to this channel" in response.json()["detail"]


def test_changed_replay_and_stale_save_preserve_current_draft(saved):
    client, channel, _ = saved
    url = root(channel)
    project = client.post(url, json=brief()).json()
    changed = brief()
    changed["brief"]["prompt"] = "Different project"
    assert client.post(url, json=changed).status_code == 409
    revision_url = f"{url}/{project['id']}/revisions"
    body = {"expected_revision": 0, "idempotency_key": "save-1", "draft": draft()}
    assert client.post(revision_url, json=body).status_code == 201
    body["draft"]["script"][0]["narration"] = "Changed draft"
    assert client.post(revision_url, json=body).status_code == 409
    body["idempotency_key"] = "save-2"
    assert client.post(revision_url, json=body).status_code == 409
    assert client.get(f"{url}/{project['id']}").json()["revision"] == 1


def test_auth_channel_boundaries_and_readonly(saved):
    client, channel, other = saved
    assert client.post(root(other), json=brief()).status_code == 403
    assert client.get(root(other)).status_code == 403
    project = client.post(root(channel), json=brief()).json()
    assert client.get(f"{root(other)}/{project['id']}").status_code == 403
    client.headers["Authorization"] = "Bearer editorial-reader-token-00001"
    assert client.get(root(channel)).status_code == 200
    assert client.post(root(channel), json=brief("other")).status_code == 403
    assert (
        client.post(
            f"{root(channel)}/{project['id']}/revisions",
            json={
                "expected_revision": 0,
                "idempotency_key": "a",
                "draft": draft(),
            },
        ).status_code
        == 403
    )
    client.headers.pop("Authorization")
    assert client.get(root(channel)).status_code == 401


@pytest.mark.parametrize(
    "change",
    [
        lambda d: d["claims"][0].update(source_ids=["invented"]),
        lambda d: d["claims"][0].update(observation_ids=["invented"]),
        lambda d: d["script"][0].update(claim_ids=["invented"]),
        lambda d: d["script"][0].update(uncertainty_disclosure=None),
        lambda d: d["claims"][0].update(classification="confirmed"),
        lambda d: d["claims"][0].update(verification="rejected", verification_note="False"),
        lambda d: d["claims"][0].update(confidence=float("nan")),
        lambda d: d["observations"][0].update(end_seconds=91),
        lambda d: d["observations"][0].update(start_seconds=12),
        lambda d: d["sources"].append(d["sources"][0]),
        lambda d: d["script"][0].update(nonfactual=True),
        lambda d: d.update(approved=True),
    ],
)
def test_invalid_evidence_cannot_be_saved_as_coherent_draft(change):
    value = draft()
    change(value)
    with pytest.raises(ValidationError):
        EditorialDraft.model_validate(value)


def test_unknown_source_observation_rejected(saved):
    client, channel, _ = saved
    url = root(channel)
    project = client.post(url, json=brief()).json()
    value = draft()
    value["observations"][0]["source_url"] = "https://example.com/another-video"
    result = client.post(
        f"{url}/{project['id']}/revisions",
        json={
            "expected_revision": 0,
            "idempotency_key": "a",
            "draft": value,
        },
    )
    assert result.status_code == 409
    assert client.get(f"{url}/{project['id']}").json()["revision"] == 0


async def test_native_tool_uses_real_api_and_stable_step_identity(saved, monkeypatch):
    _, channel, _ = saved
    monkeypatch.setattr(
        goal_tools,
        "resolve_goal_authority",
        lambda goal: ({"ai:read", "production:create"}, "editorial-test-token-000001"),
    )
    monkeypatch.setattr(goal_tools, "_known_ids", lambda goal: set())
    goal = SimpleNamespace(channel_profile_id=channel, actor="principal:editor")
    step = uuid.uuid4()
    args = {"body": brief()}
    first = await goal_tools.run_native_tool(goal, "create_editorial_project", args, step)
    again = await goal_tools.run_native_tool(goal, "create_editorial_project", args, step)
    assert first["id"] == again["id"]
    assert "not connected" in first["limitation"]
    with pytest.raises(ValueError, match="not observed"):
        await goal_tools.run_native_tool(
            goal, "editorial_project", {"path": {"project_id": first["id"]}}, uuid.uuid4()
        )
    monkeypatch.setattr(goal_tools, "_known_ids", lambda goal: {first["id"]})
    detail = await goal_tools.run_native_tool(
        goal, "editorial_project", {"path": {"project_id": first["id"]}}, uuid.uuid4()
    )
    assert detail["id"] == first["id"]


def test_editorial_migration_up_down_and_constraints():
    path = Path(__file__).parents[1] / "migrations/versions/0050_editorial_projects.py"
    spec = importlib.util.spec_from_file_location("editorial_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    engine = create_engine("sqlite://")
    with (
        engine.begin() as connection,
        Operations.context(MigrationContext.configure(connection)),
    ):
        module.upgrade()
        assert "editorial_projects" in inspect(connection).get_table_names()
        assert len(inspect(connection).get_check_constraints("editorial_revisions")) == 1
        module.downgrade()
        assert "editorial_projects" not in inspect(connection).get_table_names()
    engine.dispose()


@pytest.mark.parametrize("same_request", [True, False])
def test_concurrent_saves_never_overwrite_a_revision(tmp_path, monkeypatch, same_request):
    db.load_model_metadata()
    engine = create_engine(f"sqlite:///{tmp_path / 'editorial.sqlite'}")
    db.Base.metadata.create_all(engine)
    monkeypatch.setattr(db, "SessionLocal", sessionmaker(bind=engine, expire_on_commit=False))
    channel = uuid.uuid4()
    with db.session_scope() as session:
        session.add(
            ChannelProfile(
                id=channel,
                youtube_connection_id=uuid.uuid4(),
                status="active",
                timezone="UTC",
                profile_metadata={},
            )
        )
    project = create_project(channel, CreateEditorialProject.model_validate(brief()), actor="test")
    barrier = Barrier(2)

    def write(index):
        request = SaveEditorialDraft(
            expected_revision=0,
            idempotency_key="same" if same_request else f"save-{index}",
            draft=EditorialDraft.model_validate(draft()),
        )
        barrier.wait(timeout=5)
        try:
            return save_draft(channel, project.id, request, actor="test").revision
        except EditorialConflict:
            return "conflict"

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(write, [1, 2]))
        assert results == [1, 1] if same_request else set(results) == {1, "conflict"}
        with db.session_scope() as session:
            assert session.get(EditorialProject, project.id).revision == 1
            assert len(list(session.scalars(select(EditorialRevision)))) == 1
    finally:
        engine.dispose()
