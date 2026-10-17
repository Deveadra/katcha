from __future__ import annotations

import uuid
from contextlib import contextmanager
from types import SimpleNamespace

import httpx
import pytest

from katcha.acquisition.adapters import available_adapters, get_adapter
from katcha.acquisition.youtube_discovery import (
    _provider_get_json,
    _youtube_request_auth,
    parse_youtube_candidates,
)


def test_youtube_adapter_is_registered() -> None:
    assert {item["key"] for item in available_adapters()} >= {
        "manifest",
        "rss_atom",
        "youtube",
    }
    assert get_adapter("youtube", "v1").version == "v1"


def test_parse_youtube_candidates_preserves_metrics_and_provenance() -> None:
    search_payload = {
        "items": [
            {
                "id": {"videoId": "abc123"},
                "snippet": {
                    "title": "Search title",
                    "channelTitle": "Search channel",
                    "publishedAt": "2026-09-16T01:00:00Z",
                },
            }
        ],
        "nextPageToken": "NEXT",
    }
    videos_payload = {
        "items": [
            {
                "id": "abc123",
                "snippet": {
                    "title": "Hydrated title",
                    "description": "Description",
                    "channelTitle": "Creator",
                    "channelId": "UC123",
                    "publishedAt": "2026-09-16T01:00:00Z",
                },
                "statistics": {
                    "viewCount": "12500",
                    "likeCount": "900",
                    "commentCount": "44",
                },
            }
        ]
    }

    candidates = parse_youtube_candidates(search_payload, videos_payload)

    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.external_id == "abc123"
    assert candidate.source_url == "https://www.youtube.com/watch?v=abc123"
    assert candidate.title == "Hydrated title"
    assert candidate.creator == "Creator"
    assert candidate.provenance_confidence == 0.95
    assert candidate.metadata["published_at"] == "2026-09-16T01:00:00Z"
    assert candidate.metadata["source_metrics"] == {
        "views": 12500,
        "likes": 900,
        "comments": 44,
        "search_rank": 1,
    }


def test_youtube_auth_uses_configured_api_key_without_database(monkeypatch) -> None:
    from katcha.acquisition import youtube_discovery as module

    monkeypatch.setattr(
        module,
        "get_settings",
        lambda: SimpleNamespace(youtube_data_api_key="public-data-key"),
    )

    params, headers = _youtube_request_auth({})

    assert params == {"key": "public-data-key"}
    assert headers == {}


def test_youtube_auth_uses_explicit_channel_oauth(monkeypatch) -> None:
    from katcha.acquisition import youtube_discovery as module

    connection_id = uuid.uuid4()
    settings = SimpleNamespace(youtube_data_api_key=None)
    monkeypatch.setattr(module, "get_settings", lambda: settings)
    monkeypatch.setattr(
        module,
        "get_valid_access_token",
        lambda value, settings=None: (
            "oauth-access-token"
            if value == connection_id
            else (_ for _ in ()).throw(AssertionError("wrong connection"))
        ),
    )

    params, headers = _youtube_request_auth(
        {"youtube_connection_id": str(connection_id)}
    )

    assert params == {}
    assert headers == {"Authorization": "Bearer oauth-access-token"}


def test_youtube_auth_falls_back_to_active_oauth_for_shared_source(
    monkeypatch,
) -> None:
    from katcha.acquisition import youtube_discovery as module

    connection_id = uuid.uuid4()

    class FakeSession:
        def scalar(self, _statement):
            return connection_id

    @contextmanager
    def fake_scope():
        yield FakeSession()

    monkeypatch.setattr(
        module,
        "get_settings",
        lambda: SimpleNamespace(youtube_data_api_key=None),
    )
    monkeypatch.setattr(module, "session_scope", fake_scope)
    monkeypatch.setattr(
        module,
        "get_valid_access_token",
        lambda value, settings=None: (
            "shared-oauth-token"
            if value == connection_id
            else (_ for _ in ()).throw(AssertionError("wrong connection"))
        ),
    )

    params, headers = _youtube_request_auth({})

    assert params == {}
    assert headers == {"Authorization": "Bearer shared-oauth-token"}


def test_expired_youtube_oauth_reports_precise_refresh_failure(monkeypatch):
    from katcha.acquisition import youtube_discovery as module
    from katcha.integrations.youtube.tokens import YouTubeCredentialError

    connection_id = uuid.uuid4()
    monkeypatch.setattr(
        module, "get_settings",
        lambda: SimpleNamespace(youtube_data_api_key=None),
    )
    monkeypatch.setattr(
        module, "get_valid_access_token",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            YouTubeCredentialError("YouTube OAuth client is not configured")
        ),
    )
    with pytest.raises(ValueError, match="OAuth client is not configured"):
        _youtube_request_auth({"youtube_connection_id": str(connection_id)})


def test_discovery_falls_back_to_key_when_explicit_oauth_cannot_refresh(monkeypatch):
    from katcha.acquisition import youtube_discovery as module
    from katcha.integrations.youtube.tokens import YouTubeCredentialError

    monkeypatch.setattr(
        module, "get_settings",
        lambda: SimpleNamespace(youtube_data_api_key="safe-public-key"),
    )
    monkeypatch.setattr(
        module, "get_valid_access_token",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            YouTubeCredentialError("YouTube OAuth revoked")
        ),
    )
    assert _youtube_request_auth({"youtube_connection_id": str(uuid.uuid4())}) == (
        {"key": "safe-public-key"}, {}
    )


@pytest.mark.parametrize("selector", ["@marvel", "https://www.youtube.com/@marvel/videos"])
def test_youtube_verification_uses_one_unit_and_never_searches(monkeypatch, selector):
    from katcha.acquisition import youtube_discovery as module

    calls = []
    monkeypatch.setattr(
        module, "_youtube_request_auth",
        lambda query: ({"key": "test-secret"}, {}),
    )

    class FakeClient:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

    monkeypatch.setattr(module.httpx, "Client", FakeClient)

    def fake_get(client, url, *, params, operation, headers=None, provider_usage=None):
        calls.append((url, params, operation, provider_usage))
        return {"items": [{"id": "UC" + "a" * 22}]}

    monkeypatch.setattr(module, "_provider_get_json", fake_get)
    outcome = module.verify_youtube_discovery({"channel_reference": selector})
    assert outcome == {
        "status": "verified",
        "authentication": "api_key",
        "operation": "channels.list",
        "quota_units": 1,
        "channel_found": True,
    }
    assert len(calls) == 1
    assert calls[0][0] == module._CHANNELS_URL
    assert calls[0][1]["forHandle"] == "@marvel"
    assert calls[0][3] == {"youtube.core": 1}


def test_youtube_provider_error_does_not_expose_api_key() -> None:
    api_key = "secret-youtube-key"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, request=request, json={"error": "forbidden"})

    with (
        httpx.Client(transport=httpx.MockTransport(handler)) as client,
        pytest.raises(RuntimeError) as exc_info,
    ):
        _provider_get_json(
            client,
            "https://www.googleapis.com/youtube/v3/search",
            params={"key": api_key, "part": "snippet"},
            operation="search",
        )

    assert api_key not in str(exc_info.value)
    assert "status 403" in str(exc_info.value)


@pytest.mark.parametrize("reference", [
    "@creator", "https://www.youtube.com/@creator/videos",
])
def test_channel_watch_resolves_handle_and_scopes_video_search(monkeypatch, reference):
    from katcha.acquisition import youtube_discovery as module

    channel_id = "UC" + "a" * 22
    requests = []

    def handler(request):
        requests.append(request)
        if request.url.path.endswith("/channels"):
            assert request.url.params["forHandle"] == "@creator"
            return httpx.Response(200, json={"items": [{"id": channel_id}]})
        if request.url.path.endswith("/search"):
            assert request.url.params["channelId"] == channel_id
            assert "q" not in request.url.params
            return httpx.Response(200, json={
                "items": [{"id": {"videoId": "one"}}], "nextPageToken": "next",
            })
        return httpx.Response(200, json={"items": [{
            "id": "one", "snippet": {"channelId": channel_id, "channelTitle": "Creator"},
            "statistics": {"viewCount": "100"},
        }]})

    client_type = httpx.Client
    monkeypatch.setattr(
        module, "get_settings", lambda: SimpleNamespace(youtube_data_api_key="test")
    )
    monkeypatch.setattr(module.httpx, "Client", lambda **kwargs: client_type(
        transport=httpx.MockTransport(handler), **kwargs,
    ))
    adapter = module.YouTubeDiscoveryAdapter()
    batch = adapter.discover({"channel_reference": reference}, {})
    assert len(batch.items) == 1
    assert batch.items[0].creator == "Creator"
    assert batch.items[0].metadata["source_metrics"]["views"] == 100
    assert batch.provider_usage == {"youtube.search.list": 1, "youtube.core": 2}
    assert batch.next_cursor["channel_id"] == channel_id
    requests.clear()
    next_batch = adapter.discover({"channel_reference": reference}, batch.next_cursor)
    assert len(requests) == 2
    assert next_batch.provider_usage["youtube.core"] == 1


def test_explicit_channel_search_can_scan_all_time(monkeypatch) -> None:
    from katcha.acquisition import youtube_discovery as module

    channel_id = "UC" + "b" * 22

    def handler(request):
        if request.url.path.endswith("/channels"):
            return httpx.Response(200, json={"items": [{"id": channel_id}]})
        if request.url.path.endswith("/search"):
            assert request.url.params["channelId"] == channel_id
            assert request.url.params["q"] == "VisionQuest official trailer"
            assert "publishedAfter" not in request.url.params
            return httpx.Response(
                200,
                json={"items": [{"id": {"videoId": "visionquest"}}]},
            )
        return httpx.Response(
            200,
            json={
                "items": [
                    {
                        "id": "visionquest",
                        "snippet": {
                            "title": "VisionQuest Official Trailer",
                            "channelId": channel_id,
                            "channelTitle": "Marvel Entertainment",
                        },
                        "statistics": {},
                    }
                ]
            },
        )

    client_type = httpx.Client
    monkeypatch.setattr(
        module,
        "get_settings",
        lambda: SimpleNamespace(youtube_data_api_key="test"),
    )
    monkeypatch.setattr(
        module.httpx,
        "Client",
        lambda **kwargs: client_type(
            transport=httpx.MockTransport(handler),
            **kwargs,
        ),
    )

    batch = module.YouTubeDiscoveryAdapter().discover(
        {
            "channel_reference": "@creator",
            "q": "VisionQuest official trailer",
            "freshness_horizon_hours": 0,
        },
        {},
    )

    assert len(batch.items) == 1
    assert batch.items[0].creator == "Marvel Entertainment"


@pytest.mark.parametrize("reference", [
    "https://evil.example/@creator", "https://www.youtube.com/watch?v=abc",
    "http://127.0.0.1/@creator", "https://user:password@youtube.com/@creator", "creator",
])
def test_channel_watch_rejects_ambiguous_or_non_channel_references(reference):
    from katcha.acquisition.youtube_discovery import youtube_channel_selector

    with pytest.raises(ValueError):
        youtube_channel_selector(reference)
