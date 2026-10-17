import uuid
from contextlib import contextmanager

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from katcha.api import control_auth, main, studio
from katcha.clip_lifecycle_models import ClipLifecycle
from katcha.config import Settings
from katcha.models import Clip
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
    Clip.__table__.create(engine)
    ClipLifecycle.__table__.create(engine)
    clip_id = uuid.uuid4()
    channel, other_channel = uuid.uuid4(), uuid.uuid4()
    ids = []
    with Session(engine) as session:
        session.add(Clip(
            id=clip_id, sha256="a" * 64, storage_key="clip/source.mp4",
            extension="mp4", status="ready",
        ))
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
        data = b"private-draft-bytes"

        def exists(self, key):
            storage_reads.append(key)
            return True

        def stat(self, key):
            return {"size_bytes": len(self.data), "content_type": "video/mp4"}

        def iter_bytes(self, key):
            yield self.data

        def iter_range(self, key, start, end):
            yield self.data[start:end + 1]

    monkeypatch.setattr(main, "session_scope", sessions)
    monkeypatch.setattr(studio, "ObjectStore", Store)
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
    app.add_api_route(
        "/v1/productions/{production_id}/media-session",
        main.create_production_media_session,
        methods=["POST"],
    )
    app.add_api_route("/v1/clips/{clip_id}/media", main.get_clip_media)
    app.add_api_route(
        "/v1/clips/{clip_id}/media-session",
        main.create_clip_media_session,
        methods=["POST"],
    )
    app.state.clip_id = clip_id
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
    assert detail.review_url == f"/channels?channel={detail.production.channel_profile_id}#content"


def test_missing_stored_video_returns_recovery_message(draft_client, monkeypatch):
    client, ids, _ = draft_client
    monkeypatch.setattr(studio.ObjectStore, "exists", lambda self, key: False)
    result = client.get(
        f"/v1/productions/{ids[0]}/media",
        headers={"Authorization": "Bearer draft-reviewer-fixture-token-0001"},
    )
    assert result.status_code == 404
    assert "Retry loading" in result.json()["detail"]


def test_production_video_seek_ranges_are_partial_and_authorized(draft_client):
    client, ids, reads = draft_client
    path = f"/v1/productions/{ids[0]}/media"
    token = {"Authorization": "Bearer draft-reviewer-fixture-token-0001"}
    middle = client.get(path, headers={**token, "Range": "bytes=8-12"})
    assert middle.status_code == 206
    assert middle.content == b"draft"
    assert middle.headers["content-range"] == "bytes 8-12/19"
    assert middle.headers["content-length"] == "5"
    assert middle.headers["accept-ranges"] == "bytes"
    tail = client.get(path, headers={**token, "Range": "bytes=-5"})
    assert tail.status_code == 206
    assert tail.content == b"bytes"
    assert tail.headers["content-range"] == "bytes 14-18/19"
    outside = client.get(path, headers={**token, "Range": "bytes=99-"})
    assert outside.status_code == 416
    assert outside.headers["content-range"] == "bytes */19"
    assert len(reads) == 3


def test_production_review_session_allows_native_seek_without_bearer(draft_client):
    client, ids, _ = draft_client
    path = f"/v1/productions/{ids[0]}/media"
    issue = client.post(
        f"/v1/productions/{ids[0]}/media-session",
        headers={"Authorization": "Bearer draft-reviewer-fixture-token-0001"},
    )
    assert issue.status_code == 200
    assert issue.json()["media_url"] == path
    assert "httponly" in issue.headers["set-cookie"].lower()
    assert "samesite=strict" in issue.headers["set-cookie"].lower()
    assert f"path={path}" in issue.headers["set-cookie"].lower()
    seek = client.get(path, headers={"Range": "bytes=5-7"})
    assert seek.status_code == 206
    assert seek.content == b"te-"
    assert seek.headers["content-range"] == "bytes 5-7/19"


def test_forged_private_review_cookie_is_denied_before_storage(draft_client):
    client, ids, reads = draft_client
    client.cookies.set(
        "katcha_review_media", "forged",
        path=f"/v1/productions/{ids[0]}/media",
    )
    response = client.get(
        f"/v1/productions/{ids[0]}/media",
        headers={"Range": "bytes=2-3"},
    )
    assert response.status_code == 403
    assert reads == []


def test_review_media_session_cannot_cross_channel(draft_client):
    client, ids, reads = draft_client
    response = client.post(
        f"/v1/productions/{ids[1]}/media-session",
        headers={"Authorization": "Bearer draft-reviewer-fixture-token-0001"},
    )
    assert response.status_code == 403
    assert reads == []


def test_clip_review_uses_scoped_seekable_session(draft_client):
    client, _, _ = draft_client
    clip_id = client.app.state.clip_id
    path = f"/v1/clips/{clip_id}/media"
    denied = client.get(path, headers={"Range": "bytes=4-8"})
    assert denied.status_code == 401
    session = client.post(
        f"/v1/clips/{clip_id}/media-session",
        headers={"Authorization": "Bearer draft-reviewer-fixture-token-0001"},
    )
    assert session.status_code == 200
    assert session.json()["media_url"] == path
    assert "httponly" in session.headers["set-cookie"].lower()
    assert f"path={path}" in session.headers["set-cookie"].lower()
    middle = client.get(path, headers={"Range": "bytes=4-8"})
    assert middle.status_code == 206
    assert middle.content == b"ate-d"
    assert middle.headers["content-range"] == "bytes 4-8/19"


def test_clip_delete_invalidates_prior_private_playback_cookie(
    draft_client, monkeypatch,
):
    from katcha.clip_lifecycle_models import ClipLifecycle

    client, _, _ = draft_client
    clip_id = client.app.state.clip_id
    session = client.post(
        f"/v1/clips/{clip_id}/media-session",
        headers={"Authorization": "Bearer draft-reviewer-fixture-token-0001"},
    )
    assert session.status_code == 200
    # Lifecycle purge must be rechecked for each requested seek.
    original = main._clip_review_asset

    def purged(clip):
        if clip == clip_id:
            raise main.HTTPException(
                410, "clip media was permanently purged"
            )
        return original(clip)

    monkeypatch.setattr(main, "_clip_review_asset", purged)
    denied = client.get(f"/v1/clips/{clip_id}/media", headers={"Range": "bytes=3-5"})
    assert denied.status_code == 410
