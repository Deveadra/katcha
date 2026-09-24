from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import httpx
import pytest

from katcha.acquisition.adapters import DiscoveryProviderError
from katcha.acquisition.http_errors import (
    parse_retry_after,
    provider_response_error,
    provider_transport_error,
)
from katcha.acquisition.youtube_discovery import _provider_get_json


def test_retry_after_supports_seconds_and_http_date() -> None:
    now = datetime(2026, 9, 23, 20, 0, tzinfo=UTC)
    assert parse_retry_after("120", now=now) == 120
    assert parse_retry_after("Wed, 23 Sep 2026 20:02:00 GMT", now=now) == 120
    assert parse_retry_after("Wed, 23 Sep 2026 19:59:00 GMT", now=now) == 0
    assert parse_retry_after("nonsense", now=now) is None
    assert parse_retry_after("-10", now=now) is None


@pytest.mark.parametrize(
    ("status", "kind", "transient"),
    [
        (429, "rate_limited", True),
        (503, "provider_unavailable", True),
        (401, "provider_configuration", False),
        (403, "provider_configuration", False),
        (400, "provider_error", False),
    ],
)
def test_http_error_classification_is_secret_safe(
    status: int, kind: str, transient: bool
) -> None:
    request = httpx.Request(
        "GET", "https://example.com/api?key=do-not-expose",
        headers={"Authorization": "Bearer do-not-expose"},
    )
    response = httpx.Response(
        status,
        request=request,
        headers={"Retry-After": "120"},
        text="do-not-expose response body",
    )
    error = provider_response_error("Example", "search", response)
    assert isinstance(error, DiscoveryProviderError)
    assert error.kind == kind
    assert error.transient == transient
    assert error.status_code == status
    assert error.retry_after_seconds == 120
    assert "do-not-expose" not in str(error)
    assert f"status {status}" in str(error)


def test_transport_error_message_does_not_echo_exception_or_url() -> None:
    error = provider_transport_error("Example", "search")
    assert error.kind == "transport_error"
    assert error.transient is True
    assert "https://" not in str(error)


def test_youtube_http_429_retains_retry_after_without_api_key() -> None:
    secret = "super-secret-youtube-key"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            429,
            request=request,
            headers={"Retry-After": "240"},
            json={"error": secret},
        )

    with (
        httpx.Client(transport=httpx.MockTransport(handler)) as client,
        pytest.raises(DiscoveryProviderError) as raised,
    ):
        _provider_get_json(
            client,
            "https://www.googleapis.com/youtube/v3/search",
            params={"key": secret, "part": "snippet"},
            operation="search",
            provider_usage={"youtube.search.list": 1},
        )
    assert raised.value.kind == "rate_limited"
    assert raised.value.retry_after_seconds == 240
    assert raised.value.provider_usage == {"youtube.search.list": 1}
    assert secret not in str(raised.value)


def test_reddit_oauth_429_retains_retry_after(monkeypatch: pytest.MonkeyPatch) -> None:
    from katcha.acquisition import reddit_discovery as reddit

    monkeypatch.setattr(
        reddit,
        "get_settings",
        lambda: SimpleNamespace(
            reddit_client_id="id",
            reddit_client_secret="secret-oauth",
            reddit_user_agent="Katcha-test",
        ),
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            429, request=request,
            headers={"Retry-After": "180"},
            text="secret-oauth",
        )

    with (
        httpx.Client(transport=httpx.MockTransport(handler)) as client,
        pytest.raises(DiscoveryProviderError) as raised,
    ):
        reddit.RedditDiscoveryAdapter()._access_token(client)
    assert raised.value.kind == "rate_limited"
    assert raised.value.retry_after_seconds == 180
    assert raised.value.provider_usage == {"reddit.oauth": 1}
    assert "secret-oauth" not in str(raised.value)


def test_reddit_search_503_is_structured(monkeypatch: pytest.MonkeyPatch) -> None:
    from katcha.acquisition import reddit_discovery as reddit

    monkeypatch.setattr(
        reddit, "get_settings",
        lambda: SimpleNamespace(reddit_user_agent="Katcha-test"),
    )
    client_type = httpx.Client

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, request=request, text="private response")

    monkeypatch.setattr(
        reddit.httpx,
        "Client",
        lambda **kwargs: client_type(transport=httpx.MockTransport(handler)),
    )
    adapter = reddit.RedditDiscoveryAdapter()
    monkeypatch.setattr(adapter, "_access_token", lambda client: "secret-access")
    with pytest.raises(DiscoveryProviderError) as raised:
        adapter.discover({"q": "gaming"}, {})
    assert raised.value.status_code == 503
    assert raised.value.kind == "provider_unavailable"
    assert "secret-access" not in str(raised.value)


def test_rss_429_retains_retry_after_without_leaking_feed_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from katcha.acquisition import feeds

    monkeypatch.setattr(feeds, "_validate_public_url", lambda url: None)
    client_type = httpx.Client

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            429, request=request, headers={"Retry-After": "90"},
            text="private feed response",
        )

    monkeypatch.setattr(
        feeds.httpx,
        "Client",
        lambda **kwargs: client_type(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(DiscoveryProviderError) as raised:
        feeds._fetch_feed("https://example.com/rss?secret=hidden")
    assert raised.value.kind == "rate_limited"
    assert raised.value.retry_after_seconds == 90
    assert raised.value.provider_usage == {"rss.http": 1}
    assert "hidden" not in str(raised.value)
    assert "private feed response" not in str(raised.value)
