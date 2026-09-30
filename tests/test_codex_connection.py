import json
import uuid
from contextlib import contextmanager
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import httpx

from katcha.config import Settings
from katcha.integrations import codex


def _settings() -> Settings:
    return Settings(
        credential_encryption_key="MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA=",
    )


def _session() -> codex.CodexSession:
    return codex.CodexSession(
        connection_id=uuid.uuid4(),
        access_token="oauth-token",
        account_id="acct_test",
        model="gpt-5.5",
        email="person@example.com",
        plan_type="plus",
    )


def test_codex_oauth_matches_openai_codex_flow(monkeypatch) -> None:
    added = []

    @contextmanager
    def fake_scope():
        yield SimpleNamespace(add=added.append)

    monkeypatch.setattr(codex, "session_scope", fake_scope)
    monkeypatch.setattr(codex, "encrypt_secret", lambda value, settings: "encrypted")

    url = codex.begin_codex_oauth(_settings())
    parsed = urlparse(url)
    query = parse_qs(parsed.query)

    assert parsed.scheme == "https"
    assert parsed.netloc == "auth.openai.com"
    assert parsed.path == "/oauth/authorize"
    assert query["client_id"] == ["app_EMoamEEZ73f0CkXaXp7hrann"]
    assert query["redirect_uri"] == ["http://localhost:1457/auth/callback"]
    assert query["codex_cli_simplified_flow"] == ["true"]
    assert query["originator"] == ["katcha"]
    assert query["scope"] == ["openid profile email offline_access"]
    assert added


def test_codex_usage_reads_wham_windows(monkeypatch) -> None:
    monkeypatch.setattr(codex, "active_session", lambda settings=None: _session())

    def get(url, **kwargs):
        assert url == "https://chatgpt.com/backend-api/wham/usage"
        assert kwargs["headers"]["ChatGPT-Account-Id"] == "acct_test"
        return httpx.Response(
            200,
            json={
                "plan_type": "plus",
                "rate_limit": {
                    "primary_window": {
                        "used_percent": 22.5,
                        "limit_window_seconds": 18000,
                        "reset_at": 2000000000,
                    },
                    "secondary_window": {
                        "used_percent": 61.0,
                        "limit_window_seconds": 604800,
                        "reset_at": 2000500000,
                    },
                },
            },
            request=httpx.Request("GET", url),
        )

    monkeypatch.setattr(codex.httpx, "get", get)
    result = codex.usage()

    assert result["plan_type"] == "plus"
    assert result["primary"]["used_percent"] == 22.5
    assert result["primary"]["window_minutes"] == 300
    assert result["secondary"]["window_minutes"] == 10080


class _StreamContext:
    def __init__(self, response: httpx.Response) -> None:
        self.response = response

    def __enter__(self) -> httpx.Response:
        return self.response

    def __exit__(self, *_args) -> bool:
        self.response.close()
        return False


def test_codex_inference_uses_subscription_backend_and_account_header(monkeypatch) -> None:
    captured = {}

    body = "".join(
        [
            "data: "
            + json.dumps(
                {"type": "response.output_text.delta", "delta": '{"answer":"hello"}'}
            )
            + "\n\n",
            "data: "
            + json.dumps(
                {
                    "type": "response.completed",
                    "response": {
                        "usage": {"input_tokens": 15, "output_tokens": 6}
                    },
                }
            )
            + "\n\n",
        ]
    )

    def stream(method, url, **kwargs):
        captured["method"] = method
        captured["url"] = url
        captured.update(kwargs)
        return _StreamContext(
            httpx.Response(
                200,
                content=body.encode(),
                request=httpx.Request(method, url),
            )
        )

    monkeypatch.setattr(codex, "active_session", lambda settings=None: _session())
    monkeypatch.setattr(codex.httpx, "stream", stream)

    result = codex.invoke_json(
        prompt="hello",
        schema_name="answer",
        schema={"type": "object", "properties": {"answer": {"type": "string"}}},
    )

    assert result.text == '{"answer":"hello"}'
    assert result.input_tokens == 15
    assert result.output_tokens == 6
    assert captured["url"] == "https://chatgpt.com/backend-api/codex/responses"
    assert captured["headers"]["Authorization"] == "Bearer oauth-token"
    assert captured["headers"]["ChatGPT-Account-Id"] == "acct_test"
    assert captured["headers"]["originator"] == "katcha"
    assert captured["json"]["store"] is False
    assert captured["json"]["stream"] is True
    assert "Return only valid JSON" in captured["json"]["input"][0]["content"][0]["text"]
