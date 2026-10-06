import uuid
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from fastapi import HTTPException, Response
from starlette.requests import Request

import katcha.api.main as api_main
from katcha.api.main import app
from katcha.clip_lifecycle_models import ClipLifecycle
from katcha.models import Clip


def test_clip_library_lifecycle_routes_are_registered() -> None:
    paths = app.openapi()["paths"]
    expected = {
        "/v1/clips/library",
        "/v1/clips/library/summary",
        "/v1/clips/{clip_id}/media",
        "/v1/clips/{clip_id}/media-session",
        "/v1/clips/{clip_id}/sources",
        "/v1/clips/{clip_id}/library-state",
        "/v1/clips/{clip_id}/library-metadata",
        "/v1/clips/{clip_id}/archive",
        "/v1/clips/{clip_id}/restore",
        "/v1/clips/{clip_id}/purge",
        "/v1/channels/{channel_profile_id}/clip-retention",
        "/v1/channels/{channel_profile_id}/clip-retention/preview",
        "/v1/channels/{channel_profile_id}/clip-retention/run",
    }
    assert expected <= set(paths)
    assert "get" in paths["/v1/clips/{clip_id}/media"]
    assert "post" in paths["/v1/clips/{clip_id}/media-session"]
    assert "patch" in paths["/v1/clips/{clip_id}/library-metadata"]
    assert "post" in paths["/v1/clips/{clip_id}/purge"]
    assert "put" in paths["/v1/channels/{channel_profile_id}/clip-retention"]


def _media_request(
    range_value: str | None = None,
    *,
    method: str = "GET",
    path: str = "/v1/clips/test/media",
    scheme: str = "http",
) -> Request:
    headers = []
    if range_value:
        headers.append((b"range", range_value.encode()))
    return Request(
        {
            "type": "http",
            "method": method,
            "path": path,
            "raw_path": path.encode(),
            "headers": headers,
            "query_string": b"",
            "scheme": scheme,
            "server": ("testserver", 80),
            "client": ("127.0.0.1", 12345),
        }
    )


@pytest.mark.parametrize(
    ("value", "size", "expected"),
    [
        ("bytes=0-3", 10, (0, 3)),
        ("bytes=4-", 10, (4, 9)),
        ("bytes=-4", 10, (6, 9)),
        ("bytes=0-999", 10, (0, 9)),
    ],
)
def test_clip_media_range_parsing(value: str, size: int, expected: tuple[int, int]) -> None:
    assert api_main._clip_media_range(value, size) == expected


@pytest.mark.parametrize("value", ["items=0-3", "bytes=9-2", "bytes=10-", "bytes=0-1,3-4"])
def test_clip_media_range_rejects_invalid_requests(value: str) -> None:
    with pytest.raises(HTTPException) as exc:
        api_main._clip_media_range(value, 10)
    assert exc.value.status_code == 416
    assert exc.value.headers == {"Content-Range": "bytes */10"}


@pytest.mark.asyncio
async def test_clip_media_streams_only_authorized_channel_range(monkeypatch) -> None:
    clip_id = uuid.uuid4()
    channel_id = uuid.uuid4()
    data = b"0123456789"
    clip = SimpleNamespace(storage_key="raw/test.mp4", extension="mp4")

    class FakeSession:
        def get(self, model, identity):
            if model is Clip and identity == clip_id:
                return clip
            if model is ClipLifecycle:
                return None
            return None

    @contextmanager
    def fake_session_scope():
        yield FakeSession()

    class FakeStore:
        def exists(self, key):
            return key == "raw/test.mp4"

        def stat(self, key):
            return {
                "size_bytes": len(data),
                "content_type": "video/mp4",
                "etag": "fixture",
            }

        def iter_range(self, key, start, end):
            yield data[start : end + 1]

        def iter_bytes(self, key):
            yield data

    monkeypatch.setattr(api_main, "session_scope", fake_session_scope)
    monkeypatch.setattr(api_main, "ObjectStore", FakeStore)
    monkeypatch.setattr(
        api_main,
        "channel_ids_for_clip",
        lambda session, value: {channel_id} if value == clip_id else set(),
    )

    response = api_main.get_clip_media(
        clip_id,
        _media_request("bytes=2-5"),
        channel_profile_id=channel_id,
    )
    assert response.status_code == 206
    assert response.headers["accept-ranges"] == "bytes"
    assert response.headers["content-range"] == "bytes 2-5/10"
    assert response.headers["content-length"] == "4"
    assert response.media_type == "video/mp4"
    assert b"".join([chunk async for chunk in response.body_iterator]) == b"2345"

    with pytest.raises(HTTPException) as exc:
        api_main.get_clip_media(
            clip_id,
            _media_request(),
            channel_profile_id=uuid.uuid4(),
        )
    assert exc.value.status_code == 404

    cookie_response = Response()
    value = api_main.create_clip_media_session(
        clip_id,
        channel_id,
        _media_request(
            method="POST",
            path=f"/v1/clips/{clip_id}/media-session",
            scheme="https",
        ),
        cookie_response,
    )
    assert value["media_url"] == (
        f"/v1/clips/{clip_id}/media?channel_profile_id={channel_id}"
    )
    cookie = cookie_response.headers["set-cookie"]
    assert "katcha_media_playback=" in cookie
    assert "HttpOnly" in cookie
    assert "SameSite=strict" in cookie
    assert "Secure" in cookie
    assert f"Path=/v1/clips/{clip_id}/media" in cookie
    assert cookie_response.headers["cache-control"] == "no-store"
