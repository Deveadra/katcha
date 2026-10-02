"""Synthetic evidence tests; no live research or factual-verification claim."""

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
from katcha.editorial.project_schemas import (
    CreateEditorialProject,
    EditorialDraft,
    SaveEditorialDraft,
)
from katcha.editorial_models import EditorialProject, EditorialRevision
from katcha.intelligence_models import ChannelProfile
from katcha.models import DomainEvent
from katcha.services import goal_tools
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
