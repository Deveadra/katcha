"""Editorial preview sessions must stream private byte ranges without full Blob loads."""

import uuid
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from test_editorial_projects import root, saved

from katcha.api import main, studio

DATA = b"private-editorial-render-payload"


@pytest.fixture
def preview(saved, monkeypatch):
    client, channel, other = saved
    project_id, run_id = uuid.uuid4(), uuid.uuid4()
    base = f"{root(channel)}/{project_id}/runs/{run_id}"
    reads = []

    class Store:
        def exists(self, key):
            reads.append(("exists", key))
            return True

        def stat(self, key):
            return {"size_bytes": len(DATA), "content_type": "video/mp4"}

        def iter_bytes(self, key):
            reads.append(("full", key))
            yield DATA

        def iter_range(self, key, start, end):
            reads.append(("range", key, start, end))
            yield DATA[start : end + 1]

    monkeypatch.setattr(studio, "ObjectStore", Store)
    monkeypatch.setattr(
        "katcha.services.editorial_runs.get_run",
        lambda *args: SimpleNamespace(id=run_id, project_id=project_id),
    )
    monkeypatch.setattr(
        "katcha.services.editorial_reviews.verified_manifest",
        lambda row: SimpleNamespace(output_key="renders/editorial-private.mp4"),
    )
    return client, channel, other, base, reads


def test_preview_session_mints_scoped_cookie_and_seeks_only_required_bytes(preview):
    client, _, _, base, reads = preview
    path = base + "/preview"
    assert client.get(path, headers={"Authorization": ""}).status_code == 401
    assert reads == []

    ticket = client.post(base + "/preview-session")
    assert ticket.status_code == 200, ticket.text
    assert ticket.json()["media_url"] == path
    assert ticket.json()["expires_in_seconds"] == 3600
    cookie = ticket.headers["set-cookie"].lower()
    assert "httponly" in cookie
    assert "samesite=strict" in cookie
    assert f"path={path}" in cookie
    assert ticket.headers["cache-control"] == "no-store"

    middle = client.get(path, headers={"Authorization": "", "Range": "bytes=8-15"})
    assert middle.status_code == 206
    assert middle.content == DATA[8:16]
    assert middle.headers["accept-ranges"] == "bytes"
    assert middle.headers["content-range"] == f"bytes 8-15/{len(DATA)}"
    assert middle.headers["content-length"] == "8"
    assert reads == [
        ("exists", "renders/editorial-private.mp4"),
        ("range", "renders/editorial-private.mp4", 8, 15),
    ]

    suffix = client.get(path, headers={"Authorization": "", "Range": "bytes=-3"})
    assert suffix.status_code == 206
    assert suffix.content == DATA[-3:]
    assert ("full", "renders/editorial-private.mp4") not in reads

    invalid = client.get(path, headers={"Authorization": "", "Range": "bytes=999-"})
    assert invalid.status_code == 416
    assert invalid.headers["content-range"] == f"bytes */{len(DATA)}"


def test_editorial_preview_ticket_never_crosses_project_or_channel(preview):
    client, channel, other, base, reads = preview
    session = client.post(base + "/preview-session")
    assert session.status_code == 200
    wrong_project = f"{root(channel)}/{uuid.uuid4()}/runs/{uuid.uuid4()}/preview"
    assert client.get(wrong_project, headers={"Authorization": ""}).status_code == 401
    foreign = f"{root(other)}/{uuid.uuid4()}/runs/{uuid.uuid4()}/preview"
    assert client.get(foreign).status_code == 403
    assert reads == []


def test_forged_or_expired_ticket_denied_before_media_storage(preview, monkeypatch):
    client, _, _, base, reads = preview
    from katcha.security import media_tickets

    ticket = client.post(base + "/preview-session")
    assert ticket.status_code == 200
    path = base + "/preview"
    with TestClient(main.app) as anonymous:
        anonymous.cookies.set("katcha_review_media", "forged", path=path)
        denied = anonymous.get(path)
        assert denied.status_code == 403
    assert reads == []

    def expired(*args, **kwargs):
        raise ValueError("private media ticket is expired")

    monkeypatch.setattr(media_tickets, "verify_media_ticket", expired)
    expired_response = client.get(path, headers={"Authorization": ""})
    assert expired_response.status_code == 403
    assert reads == []


def test_editorial_preview_rechecks_current_render_clearance(preview, monkeypatch):
    client, _, _, base, reads = preview
    assert client.post(base + "/preview-session").status_code == 200

    def revoked(row):
        raise ValueError("current render clearance revoked")

    monkeypatch.setattr(
        "katcha.services.editorial_reviews.verified_manifest", revoked
    )
    denied = client.get(base + "/preview", headers={"Authorization": ""})
    assert denied.status_code != 200
    assert reads == []
