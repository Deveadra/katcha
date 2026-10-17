import uuid
from contextlib import contextmanager

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from katcha.api import control_auth, main
from katcha.config import Settings
from katcha.production_models import Production, ProductionAsset, ProductionReview, ProductionScript


@pytest.fixture
def draft_client(monkeypatch):
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Production.__table__.create(engine)
    ProductionAsset.__table__.create(engine)
    ProductionScript.__table__.create(engine)
    ProductionReview.__table__.create(engine)
    channel, other_channel = uuid.uuid4(), uuid.uuid4()
    ids = []
    with Session(engine) as session:
        for index, owner in enumerate([channel, other_channel, channel]):
            production = Production(
                clip_id=uuid.uuid4(),
                channel_profile_id=owner,
                workflow_id=str(uuid.uuid4()),
                persona_key="host",
                persona_version="1",
                prompt_version="1",
                status="review",
                render_manifest={"output_key": f"draft/{index}"},
            )
            session.add(production)
            session.flush()
            ids.append(production.id)
            if index != 2:
                session.add(
                    ProductionAsset(
                        production_id=production.id,
                        kind="render",
                        storage_key=f"draft/{index}",
                        content_type="video/mp4",
                    )
                )
        session.commit()

    @contextmanager
    def sessions():
        with Session(engine) as session:
            yield session

    storage_reads = []

    class Store:
        def exists(self, key):
            storage_reads.append(key)
            return True

        def iter_bytes(self, key):
            yield b"private-draft-bytes"

    monkeypatch.setattr(main, "session_scope", sessions)
    monkeypatch.setattr(main, "ObjectStore", Store)
    settings = Settings(
        _env_file=None,
        control_principals=[
            {
                "name": "reviewer",
                "token": "draft-reviewer-fixture-token-0001",
                "scopes": ["channels:read"],
                "channel_profile_ids": [str(channel)],
            }
        ],
    )
    monkeypatch.setattr(control_auth, "get_settings", lambda: settings)
    app = FastAPI(
        dependencies=[
            Depends(control_auth.require_control_token),
            Depends(control_auth.require_native_channel_body),
        ]
    )
    app.add_api_route("/v1/productions/{production_id}/media", main.get_production_media)
    with TestClient(app) as client:
        yield client, ids, storage_reads
    engine.dispose()


def test_finished_draft_stream_requires_auth_and_is_private(draft_client):
    client, ids, reads = draft_client
    path = f"/v1/productions/{ids[0]}/media"
    assert client.get(path).status_code == 401
    assert reads == []
    result = client.get(path, headers={"Authorization": "Bearer draft-reviewer-fixture-token-0001"})
    assert result.status_code == 200
    assert result.content == b"private-draft-bytes"
    assert result.headers["content-type"] == "video/mp4"
    assert result.headers["cache-control"] == "private, no-store"
    assert reads == ["draft/0"]


def test_draft_from_another_channel_is_denied_before_storage(draft_client):
    client, ids, reads = draft_client
    result = client.get(
        f"/v1/productions/{ids[1]}/media",
        headers={"Authorization": "Bearer draft-reviewer-fixture-token-0001"},
    )
    assert result.status_code == 403
    assert reads == []


def test_unfinished_draft_returns_recovery_state_without_reading_storage(draft_client):
    client, ids, reads = draft_client
    result = client.get(
        f"/v1/productions/{ids[2]}/media",
        headers={"Authorization": "Bearer draft-reviewer-fixture-token-0001"},
    )
    assert result.status_code == 409
    assert "no finished video" in result.json()["detail"]
    assert reads == []


def test_detail_exposes_the_channel_review_page(draft_client):
    _, ids, _ = draft_client
    detail = main.get_production(ids[0])
    assert detail.review_url == (
        f"/channels?channel={detail.production.channel_profile_id}&production={ids[0]}#content"
    )
    assert detail.production.review_url == detail.review_url
    assert main.get_production(ids[1]).review_url != detail.review_url


def test_missing_stored_video_returns_recovery_message(draft_client, monkeypatch):
    client, ids, _ = draft_client
    monkeypatch.setattr(main.ObjectStore, "exists", lambda self, key: False)
    result = client.get(
        f"/v1/productions/{ids[0]}/media",
        headers={"Authorization": "Bearer draft-reviewer-fixture-token-0001"},
    )
    assert result.status_code == 404
    assert "Retry loading" in result.json()["detail"]
