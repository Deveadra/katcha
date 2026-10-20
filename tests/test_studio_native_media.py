"""Studio native video must authorize exact resources before streaming byte ranges."""

from __future__ import annotations

import uuid
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from katcha.api import control_auth, main, studio
from katcha.config import Settings
from katcha.security import media_tickets
from katcha.short_episode_models import ShortEpisode

DATA = b"0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
TOKEN = "studio-reviewer-fixture-token-0001"


@pytest.fixture
def native_studio(monkeypatch):
    channel, other_channel, episode_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    settings = Settings(
        _env_file=None,
        control_principals=[{
            "name": "studio-reviewer",
            "token": TOKEN,
            "scopes": ["channels:read"],
            "channel_profile_ids": [str(channel)],
        }],
    )
    monkeypatch.setattr(control_auth, "get_settings", lambda: settings)
    monkeypatch.setattr(main, "get_settings", lambda: settings)
    monkeypatch.setattr(media_tickets, "get_settings", lambda: settings)

    current = {
        "owner": channel,
        "key": "episodes/verified.mp4",
        "manifest_key": "episodes/verified.mp4",
    }
    db_reads = []
    store_reads = []

    class Session:
        def get(self, model, identifier):
            db_reads.append(("get", str(identifier)))
            if model is ShortEpisode and identifier == episode_id:
                return SimpleNamespace(
                    id=episode_id,
                    channel_profile_id=current["owner"],
                    render_manifest={"output_key": current["manifest_key"]},
                )
            return None

        def scalar(self, stmt):
            db_reads.append(("scalar", "render"))
            return SimpleNamespace(
                storage_key=current["key"],
                asset_metadata={},
            )

    @contextmanager
    def sessions():
        yield Session()

    class Store:
        def exists(self, key):
            store_reads.append(("exists", key))
            return True

        def stat(self, key):
            store_reads.append(("stat", key))
            return {"size_bytes": len(DATA), "content_type": "video/mp4"}

        def iter_bytes(self, key):
            store_reads.append(("full", key))
            yield DATA

        def iter_range(self, key, start, end):
            store_reads.append(("range", key, start, end))
            yield DATA[start:end + 1]

    monkeypatch.setattr(studio, "session_scope", sessions)
    monkeypatch.setattr(studio, "ObjectStore", Store)

    app = FastAPI(dependencies=[Depends(control_auth.require_control_token)])
    app.include_router(studio.router)
    with TestClient(app) as client:
        yield client, episode_id, channel, other_channel, current, db_reads, store_reads


def path_for(episode_id):
    return f"/v1/studio/episodes/{episode_id}/media"


def test_studio_private_media_requires_authorization_before_storage(native_studio):
    client, episode_id, _, _, _, db_reads, store_reads = native_studio
    response = client.get(path_for(episode_id), headers={"Range": "bytes=4-8"})
    assert response.status_code == 401
    assert db_reads == []
    assert store_reads == []


def test_studio_native_session_uses_partial_bytes_without_blob_download(native_studio):
    client, episode_id, _, _, _, _, reads = native_studio
    path = path_for(episode_id)
    session = client.post(
        path + "-session?generation=1",
        headers={"Authorization": f"Bearer {TOKEN}"},
    )
    assert session.status_code == 200, session.text
    assert session.json()["media_url"] == path + "?generation=1"
    assert session.headers["cache-control"] == "no-store"
    cookie = session.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=strict" in cookie
    assert f"path={path}" in cookie

    result = client.get(path + "?generation=1", headers={"Range": "bytes=10-14"})
    assert result.status_code == 206
    assert result.content == DATA[10:15]
    assert result.headers["accept-ranges"] == "bytes"
    assert result.headers["content-range"] == f"bytes 10-14/{len(DATA)}"
    assert result.headers["content-length"] == "5"
    assert result.headers["cache-control"] == "private, no-store"
    assert ("full", "episodes/verified.mp4") not in reads
    assert ("range", "episodes/verified.mp4", 10, 14) in reads

    tail = client.get(path + "?generation=1", headers={"Range": "bytes=-3"})
    assert tail.status_code == 206 and tail.content == DATA[-3:]

    outside = client.get(path + "?generation=1", headers={"Range": "bytes=999-"})
    assert outside.status_code == 416
    assert outside.headers["content-range"] == f"bytes */{len(DATA)}"
    empty = client.get(path + "?generation=1", headers={"Range": "bytes=-"})
    assert empty.status_code == 416
    assert empty.headers["content-range"] == f"bytes */{len(DATA)}"


def test_studio_ticket_cannot_follow_generation_or_replaced_render(native_studio):
    client, episode_id, _, _, current, _, reads = native_studio
    path = path_for(episode_id)
    issued = client.post(path + "-session", headers={"Authorization": f"Bearer {TOKEN}"})
    assert issued.status_code == 200
    wrong_generation = client.get(path + "?generation=1")
    assert wrong_generation.status_code == 403
    assert reads == []

    current["key"] = "episodes/new-output.mp4"
    current["manifest_key"] = current["key"]
    replaced = client.get(path)
    assert replaced.status_code == 403
    assert reads == []


def test_studio_ticket_channel_and_forgery_are_denied_before_storage(native_studio):
    client, episode_id, _, other_channel, current, _, reads = native_studio
    path = path_for(episode_id)
    current["owner"] = other_channel
    unauthorized_session = client.post(
        path + "-session", headers={"Authorization": f"Bearer {TOKEN}"}
    )
    assert unauthorized_session.status_code == 403
    assert reads == []

    current["owner"] = uuid.uuid4()
    client.cookies.set(media_tickets.REVIEW_MEDIA_COOKIE, "forged", path=path)
    forged = client.get(path, headers={"Range": "bytes=0-4"})
    assert forged.status_code == 403
    assert reads == []


def test_studio_authorized_ordinary_get_streams_private_full_response(native_studio):
    client, episode_id, _, _, _, _, reads = native_studio
    result = client.get(
        path_for(episode_id), headers={"Authorization": f"Bearer {TOKEN}"}
    )
    assert result.status_code == 200
    assert result.content == DATA
    assert result.headers["content-length"] == str(len(DATA))
    assert result.headers["cache-control"] == "private, no-store"
    assert ("full", "episodes/verified.mp4") in reads
