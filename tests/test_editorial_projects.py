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
from katcha.editorial import source_monitor, source_uploads
from katcha.editorial.project_schemas import (
    CreateEditorialProject,
    EditorialDraft,
    SaveEditorialDraft,
)
from katcha.editorial_models import (
    EditorialImage,
    EditorialNarration,
    EditorialProject,
    EditorialRevision,
    EditorialRun,
    EditorialStoryboardRevision,
)
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


def storyboard_workspace(*, quote=False):
    beat = {"beat_id": "beat-1", "layout": "unassigned"}
    if quote:
        beat = {
            "beat_id": "beat-1",
            "layout": "quote",
            "quote_source_id": "source-1",
        }
    return {
        "presentation_mode": "captioned_silent",
        "asset_run_id": None,
        "beats": [beat],
        "narration_ids": {},
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


def test_storyboard_workspace_autosave_history_and_undo(saved):
    client, channel, _ = saved
    project = client.post(root(channel), json=brief("storyboard-workspace")).json()
    revision_url = f"{root(channel)}/{project['id']}/revisions"
    assert (
        client.post(
            revision_url,
            json={
                "expected_revision": 0,
                "idempotency_key": "script-1",
                "draft": draft(),
            },
        ).status_code
        == 201
    )

    workspace_url = f"{root(channel)}/{project['id']}/storyboard"
    first_body = {
        "script_revision": 1,
        "expected_version": 0,
        "idempotency_key": "board-1",
        "workspace": storyboard_workspace(),
    }
    first = client.post(workspace_url, json=first_body)
    assert first.status_code == 201, first.text
    assert first.json()["version"] == 1
    assert first.json()["parent_version"] is None
    assert first.json()["origin"] == "operator"
    assert client.post(workspace_url, json=first_body).json() == first.json()

    second_body = {
        "script_revision": 1,
        "expected_version": 1,
        "idempotency_key": "board-2",
        "workspace": storyboard_workspace(quote=True),
    }
    second = client.post(workspace_url, json=second_body)
    assert second.status_code == 201, second.text
    assert second.json()["version"] == 2
    assert second.json()["parent_version"] == 1
    assert second.json()["workspace"]["beats"][0]["layout"] == "quote"

    latest = client.get(workspace_url, params={"script_revision": 1})
    assert latest.status_code == 200
    assert latest.json()["version"] == 2
    history = client.get(
        f"{workspace_url}/history",
        params={"script_revision": 1},
    )
    assert [row["version"] for row in history.json()] == [2, 1]

    undone = client.post(
        f"{workspace_url}/undo",
        json={
            "script_revision": 1,
            "expected_version": 2,
            "idempotency_key": "undo-2",
        },
    )
    assert undone.status_code == 201, undone.text
    assert undone.json()["version"] == 3
    assert undone.json()["parent_version"] is None
    assert undone.json()["origin"] == "undo"
    assert undone.json()["workspace"]["beats"][0]["layout"] == "unassigned"

    no_more = client.post(
        f"{workspace_url}/undo",
        json={
            "script_revision": 1,
            "expected_version": 3,
            "idempotency_key": "undo-3",
        },
    )
    assert no_more.status_code == 409
    assert "No earlier Storyboard edit" in no_more.json()["detail"]

    with db.session_scope() as session:
        rows = list(
            session.scalars(
                select(EditorialStoryboardRevision).where(
                    EditorialStoryboardRevision.project_id
                    == uuid.UUID(project["id"])
                )
            )
        )
        assert len(rows) == 3
        events = list(
            session.scalars(
                select(DomainEvent).where(
                    DomainEvent.aggregate_type == "editorial_storyboard"
                )
            )
        )
        assert [event.event_type for event in events] == [
            "editorial.storyboard_saved",
            "editorial.storyboard_saved",
            "editorial.storyboard_undone",
        ]


def test_confirmed_ai_storyboard_edit_uses_history_and_rejects_stale_version(saved):
    from katcha.editorial.storyboard_schemas import StoryboardWorkspaceBeat
    from katcha.services.editorial_storyboards import apply_storyboard_beat_edit

    client, channel, _ = saved
    project = client.post(root(channel), json=brief("ai-storyboard-apply")).json()
    revision_url = f"{root(channel)}/{project['id']}/revisions"
    assert client.post(
        revision_url,
        json={
            "expected_revision": 0,
            "idempotency_key": "script-ai-1",
            "draft": draft(),
        },
    ).status_code == 201

    workspace_url = f"{root(channel)}/{project['id']}/storyboard"
    first = client.post(
        workspace_url,
        json={
            "script_revision": 1,
            "expected_version": 0,
            "idempotency_key": "board-ai-1",
            "workspace": storyboard_workspace(),
        },
    )
    assert first.status_code == 201, first.text
    assert first.json()["version"] == 1

    applied = apply_storyboard_beat_edit(
        channel,
        uuid.UUID(project["id"]),
        script_revision=1,
        expected_version=1,
        beat=StoryboardWorkspaceBeat(
            beat_id="beat-1",
            layout="quote",
            quote_source_id="source-1",
        ),
        asset_run_id=None,
        idempotency_key="goal-step:ai-edit-fixture",
        actor="control-principal:editor",
    )
    assert applied.version == 2
    assert applied.parent_version == 1
    assert applied.origin == "ai_apply"
    assert applied.workspace["beats"][0]["layout"] == "quote"
    assert applied.workspace["beats"][0]["quote_source_id"] == "source-1"

    with pytest.raises(EditorialConflict, match="Storyboard changed"):
        apply_storyboard_beat_edit(
            channel,
            uuid.UUID(project["id"]),
            script_revision=1,
            expected_version=1,
            beat=StoryboardWorkspaceBeat(
                beat_id="beat-1",
                layout="unassigned",
            ),
            asset_run_id=None,
            idempotency_key="goal-step:stale-ai-edit-fixture",
            actor="control-principal:editor",
        )

    history = client.get(
        f"{workspace_url}/history",
        params={"script_revision": 1},
    ).json()
    assert [row["version"] for row in history] == [2, 1]
    assert history[0]["origin"] == "ai_apply"


def test_storyboard_workspace_rejects_stale_script_and_unknown_evidence(saved):
    client, channel, _ = saved
    project = client.post(root(channel), json=brief("storyboard-stale")).json()
    revision_url = f"{root(channel)}/{project['id']}/revisions"
    assert client.post(
        revision_url,
        json={
            "expected_revision": 0,
            "idempotency_key": "script-1",
            "draft": draft(),
        },
    ).status_code == 201
    workspace_url = f"{root(channel)}/{project['id']}/storyboard"

    invalid = storyboard_workspace(quote=True)
    invalid["beats"][0]["quote_source_id"] = "invented"
    response = client.post(
        workspace_url,
        json={
            "script_revision": 1,
            "expected_version": 0,
            "idempotency_key": "bad-source",
            "workspace": invalid,
        },
    )
    assert response.status_code == 409
    assert "unknown research source" in response.json()["detail"]

    changed = draft()
    changed["script"][0]["narration"] = "A later saved theory."
    assert client.post(
        revision_url,
        json={
            "expected_revision": 1,
            "idempotency_key": "script-2",
            "draft": changed,
        },
    ).status_code == 201
    stale = client.post(
        workspace_url,
        json={
            "script_revision": 1,
            "expected_version": 0,
            "idempotency_key": "stale-board",
            "workspace": storyboard_workspace(),
        },
    )
    assert stale.status_code == 409
    assert "Script changed" in stale.json()["detail"]


def test_storyboard_workspace_preserves_footage_beat_lineage(saved):
    client, channel, _ = saved
    project = client.post(root(channel), json=brief("storyboard-footage")).json()
    revision_url = f"{root(channel)}/{project['id']}/revisions"
    saved_revision = client.post(
        revision_url,
        json={
            "expected_revision": 0,
            "idempotency_key": "script-1",
            "draft": draft(),
        },
    ).json()
    asset_run_id = uuid.uuid4()
    with db.session_scope() as session:
        session.add(
            EditorialRun(
                id=asset_run_id,
                project_id=uuid.UUID(project["id"]),
                channel_profile_id=channel,
                input_digest="a" * 64,
                input_revision=1,
                options={"target": "acquire_assets"},
                attempt=1,
                status="completed",
                stage="assets_acquired_for_review",
                artifacts={
                    "input_draft_digest": saved_revision["digest"],
                    "acquired_assets": {"candidate": {"clip_id": "fixture"}},
                    "asset_selection": [
                        {
                            "id": "candidate",
                            "beat_id": "beat-1",
                            "claim_ids": ["claim-1"],
                        }
                    ],
                },
                actor="test",
            )
        )

    workspace = storyboard_workspace()
    workspace["asset_run_id"] = str(asset_run_id)
    workspace["beats"][0] = {
        "beat_id": "beat-1",
        "layout": "single",
        "media": [{"candidate_id": "candidate", "start_seconds": 1.5}],
    }
    workspace_url = f"{root(channel)}/{project['id']}/storyboard"
    accepted = client.post(
        workspace_url,
        json={
            "script_revision": 1,
            "expected_version": 0,
            "idempotency_key": "media-1",
            "workspace": workspace,
        },
    )
    assert accepted.status_code == 201, accepted.text

    with db.session_scope() as session:
        run = session.get(EditorialRun, asset_run_id)
        run.artifacts = {
            **run.artifacts,
            "asset_selection": [
                {
                    "id": "candidate",
                    "beat_id": "another-beat",
                    "claim_ids": ["claim-1"],
                }
            ],
        }
    rejected = client.post(
        workspace_url,
        json={
            "script_revision": 1,
            "expected_version": 1,
            "idempotency_key": "media-2",
            "workspace": workspace,
        },
    )
    assert rejected.status_code == 409
    assert "scouted beat" in rejected.json()["detail"]


def test_storyboard_workspace_rejects_cross_beat_image_and_narration(saved):
    client, channel, _ = saved
    project = client.post(root(channel), json=brief("storyboard-revoked")).json()
    revision_url = f"{root(channel)}/{project['id']}/revisions"
    assert client.post(
        revision_url,
        json={
            "expected_revision": 0,
            "idempotency_key": "script-1",
            "draft": draft(),
        },
    ).status_code == 201
    project_id = uuid.UUID(project["id"])
    image_id = uuid.uuid4()
    narration_id = uuid.uuid4()
    with db.session_scope() as session:
        session.add(
            EditorialImage(
                id=image_id,
                project_id=project_id,
                channel_profile_id=channel,
                revision=1,
                beat_id="another-beat",
                request_digest="a" * 64,
                sha256="b" * 64,
                storage_key=f"editorial/{project_id}/1/images/{image_id}/{'b' * 64}.png",
                width=320,
                height=180,
                title="Wrong-beat still",
                source_reference="operator",
                use_note="Synthetic fixture",
                illustration=False,
                status="revoked",
                actor="test",
            )
        )
        session.add(
            EditorialNarration(
                id=narration_id,
                project_id=project_id,
                channel_profile_id=channel,
                revision=1,
                beat_id="another-beat",
                text_digest="c" * 64,
                sha256="d" * 64,
                storage_key=f"editorial/{project_id}/1/narration/beat-1/{'d' * 64}.wav",
                sample_rate=48000,
                sample_frames=96000,
                status="revoked",
                actor="test",
            )
        )

    workspace_url = f"{root(channel)}/{project['id']}/storyboard"
    image_workspace = storyboard_workspace()
    image_workspace["beats"][0] = {
        "beat_id": "beat-1",
        "layout": "image",
        "image_id": str(image_id),
    }
    image_response = client.post(
        workspace_url,
        json={
            "script_revision": 1,
            "expected_version": 0,
            "idempotency_key": "revoked-image",
            "workspace": image_workspace,
        },
    )
    assert image_response.status_code == 409
    assert "another script beat" in image_response.json()["detail"]

    narration_workspace = storyboard_workspace()
    narration_workspace["presentation_mode"] = "narrated"
    narration_workspace["narration_ids"] = {"beat-1": str(narration_id)}
    narration_response = client.post(
        workspace_url,
        json={
            "script_revision": 1,
            "expected_version": 0,
            "idempotency_key": "revoked-narration",
            "workspace": narration_workspace,
        },
    )
    assert narration_response.status_code == 409
    assert "matching saved beat" in narration_response.json()["detail"]

    with db.session_scope() as session:
        session.get(EditorialImage, image_id).beat_id = "beat-1"
        session.get(EditorialNarration, narration_id).beat_id = "beat-1"

    retained = storyboard_workspace()
    retained["presentation_mode"] = "narrated"
    retained["narration_ids"] = {"beat-1": str(narration_id)}
    retained["beats"][0] = {
        "beat_id": "beat-1",
        "layout": "image",
        "image_id": str(image_id),
    }
    retained_response = client.post(
        workspace_url,
        json={
            "script_revision": 1,
            "expected_version": 0,
            "idempotency_key": "retained-unavailable-intent",
            "workspace": retained,
        },
    )
    assert retained_response.status_code == 201, retained_response.text


def test_storyboard_migration_roundtrip():
    path = Path(__file__).parents[1] / "migrations/versions/0055_editorial_storyboards.py"
    spec = importlib.util.spec_from_file_location("storyboard_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        module.op = Operations(MigrationContext.configure(connection))
        module.upgrade()
        inspector = inspect(connection)
        assert "editorial_storyboard_revisions" in inspector.get_table_names()
        checks = {
            item["name"]
            for item in inspector.get_check_constraints(
                "editorial_storyboard_revisions"
            )
        }
        assert "ck_editorial_storyboard_version" in checks
        module.downgrade()
        assert (
            "editorial_storyboard_revisions"
            not in inspect(connection).get_table_names()
        )
    engine.dispose()


def test_source_frame_rate_requires_stable_probe_rate():
    assert source_monitor._constant_frame_rate(
        {
            "streams": [
                {
                    "codec_type": "video",
                    "avg_frame_rate": "30000/1001",
                    "r_frame_rate": "30000/1001",
                }
            ]
        }
    ) == 29.97003
    assert (
        source_monitor._constant_frame_rate(
            {
                "streams": [
                    {
                        "codec_type": "video",
                        "avg_frame_rate": "24000/1001",
                        "r_frame_rate": "30/1",
                    }
                ]
            }
        )
        is None
    )
    assert (
        source_monitor._constant_frame_rate(
            {"streams": [{"codec_type": "audio"}]}
        )
        is None
    )


def test_program_map_uses_verified_render_manifest(saved, monkeypatch):
    client, channel, _ = saved
    project_id = uuid.uuid4()
    run_id = uuid.uuid4()
    row = SimpleNamespace()
    manifest = SimpleNamespace(
        revision=3,
        version="editorial-render-v5",
        fps=30,
        output_duration_seconds=3.5,
        timeline=[
            SimpleNamespace(beat_id="beat-1", start_frame=0, duration_frames=45),
            SimpleNamespace(beat_id="beat-2", start_frame=45, duration_frames=60),
        ],
    )

    monkeypatch.setattr("katcha.services.editorial_runs.get_run", lambda *args: row)
    monkeypatch.setattr(
        "katcha.services.editorial_reviews.verified_manifest",
        lambda actual: manifest,
    )

    response = client.get(
        f"{root(channel)}/{project_id}/runs/{run_id}/program-map"
    )

    assert response.status_code == 200, response.text
    assert response.headers["Cache-Control"] == "no-store"
    assert response.json() == {
        "revision": 3,
        "manifest_version": "editorial-render-v5",
        "fps": 30,
        "output_duration_seconds": 3.5,
        "beats": [
            {
                "beat_id": "beat-1",
                "start_frame": 0,
                "duration_frames": 45,
                "end_frame": 45,
                "start_seconds": 0,
                "end_seconds": 1.5,
            },
            {
                "beat_id": "beat-2",
                "start_frame": 45,
                "duration_frames": 60,
                "end_frame": 105,
                "start_seconds": 1.5,
                "end_seconds": 3.5,
            },
        ],
    }
    assert "output_key" not in response.json()


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
            "storage_key": "raw/private-source.mp4",
            "extension": "mp4",
            "size_bytes": 12345,
            "source_media_available": True,
            "source_fps": 29.97003,
            "frame_step_available": True,
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
    assert response.json()["source_media_available"] is True
    assert response.json()["source_fps"] == 29.97003
    assert response.json()["frame_step_available"] is True
    assert response.json()["size_bytes"] == 12345
    assert "contact_sheet_key" not in response.json()
    assert "storage_key" not in response.json()


def test_storyboard_source_media_session_is_scoped_and_private(saved, monkeypatch):
    from fastapi.responses import JSONResponse

    client, channel, _ = saved
    project_id = uuid.uuid4()
    run_id = uuid.uuid4()
    calls = []

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
            "storage_key": "raw/private-source.mp4",
            "extension": "mp4",
            "size_bytes": 12345,
            "source_media_available": True,
            "coverage": "sampled_frames",
            "limitation": "Sampled frames only.",
        },
    )

    def stream(request, key, filename):
        calls.append((key, filename, request.headers.get("range")))
        return JSONResponse({"streamed": key, "filename": filename})

    monkeypatch.setattr("katcha.api.studio._stream_object", stream)
    base = (
        f"{root(channel)}/{project_id}/runs/{run_id}/assets/candidate"
    )
    session = client.post(f"{base}/source-media-session")
    assert session.status_code == 200, session.text
    assert session.json()["expires_in_seconds"] == 900
    assert session.json()["media_url"] == f"{base}/source-media"
    cookie = session.headers["set-cookie"]
    assert "HttpOnly" in cookie
    assert "SameSite=strict" in cookie
    assert f"Path={base}/source-media" in cookie
    assert session.headers["Cache-Control"] == "no-store"

    media = client.get(
        session.json()["media_url"],
        headers={"Range": "bytes=0-9"},
    )
    assert media.status_code == 200, media.text
    assert media.json()["streamed"] == "raw/private-source.mp4"
    assert calls == [
        ("raw/private-source.mp4", media.json()["filename"], "bytes=0-9")
    ]

    wrong = client.get(
        f"{root(channel)}/{project_id}/runs/{run_id}/assets/other/source-media"
    )
    assert wrong.status_code == 403


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


def test_cited_frame_api_binds_image_to_rechecked_evidence(saved, monkeypatch):
    from unittest.mock import Mock

    from fastapi.responses import Response

    client, channel, _ = saved
    base = f"{root(channel)}/{uuid.uuid4()}/runs/{uuid.uuid4()}/frames/0"
    info = {"image_key": "analysis/private/frame.jpg", "evidence_digest": "a" * 64}
    inspect = Mock(return_value=info)
    stream = Mock(return_value=Response(b"jpeg"))
    monkeypatch.setattr("katcha.editorial.frame_inspection.inspect_frame", inspect)
    monkeypatch.setattr("katcha.api.studio._stream_object", stream)
    response = client.get(base)
    assert response.status_code == 200
    assert "image_key" not in response.json()
    assert response.headers["cache-control"] == "no-store"
    assert client.get(f"{base}/image?evidence_digest={'b' * 64}").status_code == 409
    stream.assert_not_called()
    response = client.get(f"{base}/image?evidence_digest={'a' * 64}")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["content-type"] == "image/jpeg"
    assert stream.call_args.args[1] == info["image_key"]
    assert inspect.call_count == 3
