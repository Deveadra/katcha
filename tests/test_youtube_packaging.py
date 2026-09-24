from __future__ import annotations

import uuid

import httpx

from katcha.integrations.youtube import client as youtube_client


def _client(monkeypatch) -> youtube_client.YouTubeClient:
    client = youtube_client.YouTubeClient(uuid.uuid4())
    monkeypatch.setattr(client, "_headers", lambda: {"Authorization": "Bearer test"})
    return client


def test_update_snippet_preserves_required_metadata(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_put(url, **kwargs):
        captured["url"] = url
        captured.update(kwargs)
        request = httpx.Request("PUT", url)
        return httpx.Response(200, request=request, json={"id": "video-1"})

    monkeypatch.setattr(youtube_client.httpx, "put", fake_put)
    response = _client(monkeypatch).update_snippet(
        "video-1",
        title="New title",
        description="New description",
        tags=["one", "two"],
        category_id="24",
    )
    assert response["id"] == "video-1"
    assert captured["params"] == {"part": "snippet"}
    payload = captured["json"]
    assert payload["id"] == "video-1"
    assert payload["snippet"] == {
        "title": "New title",
        "description": "New description",
        "tags": ["one", "two"],
        "categoryId": "24",
    }


def test_set_thumbnail_uses_media_upload(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_post(url, **kwargs):
        captured["url"] = url
        captured.update(kwargs)
        request = httpx.Request("POST", url)
        return httpx.Response(
            200,
            request=request,
            json={"kind": "youtube#thumbnailSetResponse", "etag": "etag"},
        )

    monkeypatch.setattr(youtube_client.httpx, "post", fake_post)
    data = b"\x89PNG\r\n\x1a\nbytes"
    response = _client(monkeypatch).set_thumbnail(
        "video-1",
        data=data,
        mime_type="image/png",
    )
    assert response["kind"] == "youtube#thumbnailSetResponse"
    assert captured["params"] == {"videoId": "video-1", "uploadType": "media"}
    assert captured["content"] == data
    assert captured["headers"]["Content-Type"] == "image/png"
    assert captured["headers"]["Content-Length"] == str(len(data))
