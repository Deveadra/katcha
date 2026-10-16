import json
import uuid
from contextlib import contextmanager
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

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
            + json.dumps({"type": "response.output_text.delta", "delta": '{"answer":"hello"}'})
            + "\n\n",
            "data: "
            + json.dumps(
                {
                    "type": "response.completed",
                    "response": {"usage": {"input_tokens": 15, "output_tokens": 6}},
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
    assert captured["json"]["instructions"]
    assert "Return only valid JSON" in captured["json"]["input"][0]["content"][0]["text"]


def mock_stream(monkeypatch, events):
    body = "".join("data: " + json.dumps(event) + "\n\n" for event in events)
    monkeypatch.setattr(codex, "active_session", lambda settings=None: _session())
    monkeypatch.setattr(
        codex.httpx,
        "stream",
        lambda *args, **kwargs: _StreamContext(
            httpx.Response(
                200, content=body.encode(), request=httpx.Request("POST", "https://chatgpt.com")
            )
        ),
    )


def test_codex_accepts_completion_only_text_and_retains_search_evidence(monkeypatch):
    payload = {
        "output": [
            {
                "type": "web_search_call",
                "action": {"sources": [{"url": "https://example.com/news"}]},
            },
            {"type": "message", "content": [{"type": "output_text", "text": '{"items":[]}'}]},
        ],
        "usage": {"input_tokens": 12, "output_tokens": 4},
    }
    mock_stream(monkeypatch, [{"type": "response.completed", "response": payload}])
    result = codex.invoke_web_search_json(
        prompt="research",
        schema_name="results",
        schema={},
        tool={"type": "web_search"},
    )
    assert result.text == '{"items":[]}'
    assert result.payload == payload


@pytest.mark.parametrize("terminal", [None, "response.incomplete", "response.failed", "error"])
def test_codex_rejects_partial_or_failed_answers(monkeypatch, terminal):
    events = [{"type": "response.output_text.delta", "delta": '{"answer":"partial"}'}]
    if terminal:
        events.append({"type": terminal, "error": {"code": "fixture_failure"}})
    mock_stream(monkeypatch, events)
    with pytest.raises(codex.CodexConnectionError):
        codex.invoke_json(prompt="hello", schema_name="answer", schema={})


@pytest.mark.parametrize("catalog_outage", [False, True])
def test_successful_inference_is_not_failed_by_usage_outage(monkeypatch, catalog_outage):
    monkeypatch.setattr(codex, "active_session", lambda settings=None: _session())
    monkeypatch.setattr(codex, "list_models", lambda settings=None: [])
    monkeypatch.setattr(
        codex,
        "_stream",
        lambda *args: codex.CodexInference(
            "KATCHA_CONNECTED",
            "fixture",
            2,
            2,
        ),
    )
    monkeypatch.setattr(
        codex,
        "usage",
        lambda settings=None: (_ for _ in ()).throw(httpx.ConnectError("fixture outage")),
    )
    if catalog_outage:
        monkeypatch.setattr(
            codex,
            "list_models",
            lambda settings=None: (_ for _ in ()).throw(httpx.ConnectError("catalog outage")),
        )
    assert codex.test_connection()["ok"] is True
    assert codex.test_connection()["usage_error"]


@pytest.mark.parametrize("operation", [codex.usage, codex.list_models])
def test_codex_network_failure_has_connection_error_not_quota_error(monkeypatch, operation):
    monkeypatch.setattr(codex, "active_session", lambda settings=None: _session())

    def fail(*args, **kwargs):
        raise httpx.ConnectError("network is down")

    monkeypatch.setattr(codex.httpx, "get", fail)
    with pytest.raises(codex.CodexConnectionError, match="could not reach the provider"):
        operation()


def test_refresh_serializes_rotating_tokens_across_workers(monkeypatch):
    """Exercise the database lock with the process-local mutex deliberately absent."""
    import os
    import threading
    from concurrent.futures import ThreadPoolExecutor
    from contextlib import nullcontext
    from datetime import UTC, datetime, timedelta

    from sqlalchemy import delete

    from katcha.codex_models import CodexConnection
    from katcha.db import session_scope

    if not os.environ.get("GITHUB_ACTIONS"):
        pytest.skip("PostgreSQL concurrency acceptance runs in CI")
    connection_id = uuid.uuid4()
    with session_scope() as session:
        session.add(
            CodexConnection(
                id=connection_id,
                subject=f"fixture-{connection_id}",
                active=True,
                encrypted_access_token="old-access",
                encrypted_refresh_token="old-refresh",
                access_token_expires_at=datetime.now(UTC) - timedelta(minutes=1),
            )
        )
    entered = threading.Event()
    release = threading.Event()
    calls = []
    monkeypatch.setattr(codex, "_refresh_lock", nullcontext())
    monkeypatch.setattr(codex, "decrypt_secret", lambda value, settings: value)
    monkeypatch.setattr(codex, "encrypt_secret", lambda value, settings: value)

    def refresh(url, **kwargs):
        calls.append(kwargs["data"]["refresh_token"])
        entered.set()
        assert release.wait(5)
        return httpx.Response(
            200,
            json={"access_token": "new-access", "refresh_token": "new-refresh", "expires_in": 3600},
        )

    monkeypatch.setattr(codex.httpx, "post", refresh)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(codex._refresh, connection_id, _settings())
            assert entered.wait(5)
            second = pool.submit(codex._refresh, connection_id, _settings())
            release.set()
            assert first.result(timeout=10).encrypted_access_token == "new-access"
            assert second.result(timeout=10).encrypted_refresh_token == "new-refresh"
        assert calls == ["old-refresh"]
    finally:
        release.set()
        with session_scope() as session:
            session.execute(delete(CodexConnection).where(CodexConnection.id == connection_id))


def test_hosted_callback_completes_registered_loopback_without_fetching(monkeypatch):
    captured = {}
    connection = SimpleNamespace(id=uuid.uuid4())

    def complete(**kwargs):
        captured.update(kwargs)
        return connection

    monkeypatch.setattr(codex, "complete_codex_oauth", complete)
    result = codex.complete_codex_callback_url(
        callback_url="http://localhost:1457/auth/callback?state=browser-state&code=one-use-code",
        expected_state="browser-state",
    )
    assert result is connection
    assert captured == {"state": "browser-state", "code": "one-use-code", "settings": None}


@pytest.mark.parametrize("callback_url", [
    "https://attacker.test/auth/callback?state=browser-state&code=secret",
    "http://localhost:1457.attacker.test/auth/callback?state=browser-state&code=secret",
    "http://user@localhost:1457/auth/callback?state=browser-state&code=secret",
    "http://localhost:1457/other?state=browser-state&code=secret",
    "http://localhost:1457/auth/callback?state=browser-state&code=secret#fragment",
    "http://localhost:1457/auth/callback?state=other-browser&code=secret",
    "http://localhost:1457/auth/callback?state=browser-state&state=browser-state&code=secret",
    "http://localhost:1457/auth/callback?state=browser-state&code=secret&code=second",
    "http://localhost:1457/auth/callback?state=browser-state&code=",
    "http://localhost:1457/auth/callback?state=%E2%98%83&code=secret",
])
def test_hosted_callback_rejects_untrusted_or_ambiguous_address(monkeypatch, callback_url):
    def must_not_complete(**kwargs):
        pytest.fail("Invalid callback must never reach token exchange")

    monkeypatch.setattr(codex, "complete_codex_oauth", must_not_complete)
    with pytest.raises(codex.CodexConnectionError) as error:
        codex.complete_codex_callback_url(
            callback_url=callback_url, expected_state="browser-state"
        )
    assert "secret" not in str(error.value)
    assert callback_url not in str(error.value)


def test_hosted_callback_decline_consumes_only_matching_state(monkeypatch):
    consumed = []
    monkeypatch.setattr(codex, "consume_failed_codex_oauth", consumed.append)
    with pytest.raises(codex.CodexConnectionError, match="cancelled or declined"):
        codex.complete_codex_callback_url(
            callback_url="http://localhost:1457/auth/callback?state=browser-state&error=denied",
            expected_state="browser-state",
        )
    assert consumed == ["browser-state"]


def test_hosted_callback_preserves_expiry_and_replay_validation(monkeypatch):
    def expired(**kwargs):
        raise codex.CodexConnectionError("Codex OAuth state is expired or already consumed")

    monkeypatch.setattr(codex, "complete_codex_oauth", expired)
    with pytest.raises(codex.CodexConnectionError, match="expired or already consumed"):
        codex.complete_codex_callback_url(
            callback_url="http://localhost:1457/auth/callback?state=browser-state&code=code",
            expected_state="browser-state",
        )


@pytest.mark.parametrize(
    "status,payload",
    [
        (503, {}),
        (200, {"models": []}),
        (200, {"models": [{"slug": "hidden", "visibility": "hide"}]}),
    ],
)
def test_catalog_never_fabricates_available_models(monkeypatch, status, payload):
    monkeypatch.setattr(codex, "active_session", lambda settings=None: _session())
    monkeypatch.setattr(
        codex.httpx, "get", lambda *args, **kwargs: httpx.Response(status, json=payload)
    )
    with pytest.raises(
        codex.CodexConnectionError, match="catalog is unavailable|no available models"
    ):
        codex.list_models()


def _model_resolution_db(monkeypatch, selected="gpt-5.5"):
    row = SimpleNamespace(selected_model=selected, active=True)

    @contextmanager
    def scope():
        yield SimpleNamespace(scalar=lambda query: row)

    monkeypatch.setattr(codex, "session_scope", scope)
    return row


@pytest.mark.parametrize(
    "selected,expected", [("gpt-5.5", "account-first"), ("operator-choice", "operator-choice")]
)
def test_catalog_resolution_preserves_supported_operator_choice(monkeypatch, selected, expected):
    from dataclasses import replace

    row = _model_resolution_db(monkeypatch, selected)
    monkeypatch.setattr(
        codex,
        "_catalog_models",
        lambda info: [
            codex.CodexModel("account-first", "First"),
            codex.CodexModel("operator-choice", "Chosen"),
        ],
    )
    result = codex._resolve_model(replace(_session(), model=selected))
    assert result.model == row.selected_model == expected


def test_catalog_resolution_preserves_concurrent_operator_choice(monkeypatch):
    row = _model_resolution_db(monkeypatch, "operator-choice")
    monkeypatch.setattr(
        codex,
        "_catalog_models",
        lambda info: [
            codex.CodexModel("account-first", "First"),
            codex.CodexModel("operator-choice", "Chosen"),
        ],
    )
    assert codex._resolve_model(_session()).model == row.selected_model == "operator-choice"


def test_retired_model_retries_only_after_explicit_rejection(monkeypatch):
    row = _model_resolution_db(monkeypatch)
    catalogs = []
    calls = []
    monkeypatch.setattr(
        codex,
        "_catalog_models",
        lambda info: catalogs.append(info.model) or [codex.CodexModel("account-first", "First")],
    )

    def stream(method, url, **kwargs):
        calls.append(kwargs["json"])
        if len(calls) == 1:
            return _StreamContext(
                httpx.Response(
                    400,
                    json={
                        "detail": "The 'gpt-5.5' model is not supported when using Codex "
                        "with a ChatGPT account."
                    },
                )
            )
        body = (
            'data: {"type":"response.completed","response":{"output":'
            '[{"type":"output_text","text":"done"}]}}\n\n'
        )
        return _StreamContext(httpx.Response(200, content=body.encode()))

    monkeypatch.setattr(codex.httpx, "stream", stream)
    result = codex._stream(_session(), "request", tool={"type": "web_search"}, image_bytes=b"image")
    assert result.model == row.selected_model == "account-first"
    assert catalogs == ["gpt-5.5"]
    assert len(calls) == 2
    assert calls[0] | {"model": "account-first"} == calls[1]


@pytest.mark.parametrize(
    "status,payload",
    [
        (429, {"error": {"code": "unsupported_model"}}),
        (500, {"error": {"code": "unsupported_model"}}),
        (400, {"error": {"message": "Invalid request"}}),
    ],
)
def test_other_http_failures_never_retry_inference(monkeypatch, status, payload):
    calls = []
    monkeypatch.setattr(codex, "_catalog_models", lambda info: pytest.fail("No catalog retry"))
    monkeypatch.setattr(
        codex.httpx,
        "stream",
        lambda *args, **kwargs: (
            calls.append(kwargs) or _StreamContext(httpx.Response(status, json=payload))
        ),
    )
    with pytest.raises(codex.CodexConnectionError):
        codex._stream(_session(), "request")
    assert len(calls) == 1


def test_catalog_failure_after_model_rejection_does_not_retry(monkeypatch):
    calls = []
    monkeypatch.setattr(
        codex,
        "_catalog_models",
        lambda info: (_ for _ in ()).throw(codex.CodexConnectionError("catalog unavailable")),
    )
    monkeypatch.setattr(
        codex.httpx,
        "stream",
        lambda *args, **kwargs: (
            calls.append(kwargs)
            or _StreamContext(httpx.Response(400, json={"error": {"code": "unsupported_model"}}))
        ),
    )
    with pytest.raises(codex.CodexConnectionError, match="catalog unavailable"):
        codex._stream(_session(), "request")
    assert len(calls) == 1


def test_transport_failure_never_replays_inference(monkeypatch):
    calls = []
    monkeypatch.setattr(codex, "_catalog_models", lambda info: pytest.fail("No catalog retry"))

    def stream(*args, **kwargs):
        calls.append(kwargs)
        raise httpx.ReadTimeout("uncertain response")

    monkeypatch.setattr(codex.httpx, "stream", stream)
    with pytest.raises(codex.CodexConnectionError, match="transport failed"):
        codex._stream(_session(), "request")
    assert len(calls) == 1


@pytest.mark.parametrize(
    "saved,available,expected",
    [
        ("retired", ["new-first", "valid"], "new-first"),
        (None, ["new-first", "valid"], "new-first"),
        (None, None, ""),
        ("valid", ["new-first", "valid"], "valid"),
        ("retired", None, "retired"),
    ],
)
def test_reconnect_uses_catalog_but_retains_credentials_during_outage(
    monkeypatch, saved, available, expected
):
    from datetime import UTC, datetime, timedelta

    oauth = SimpleNamespace(
        id=uuid.uuid4(),
        consumed_at=None,
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
        encrypted_code_verifier="verifier",
        redirect_uri=codex.REDIRECT_URI,
    )
    connection = SimpleNamespace(id=uuid.uuid4(), selected_model=saved)
    lookups = iter([oauth, connection])

    @contextmanager
    def scope():
        yield SimpleNamespace(
            scalar=lambda query: next(lookups),
            get=lambda *args: oauth,
            scalars=lambda query: [],
            flush=lambda: None,
            refresh=lambda row: None,
            expunge=lambda row: None,
        )

    monkeypatch.setattr(codex, "session_scope", scope)
    monkeypatch.setattr(codex, "decrypt_secret", lambda value, settings: value)
    monkeypatch.setattr(codex, "encrypt_secret", lambda value, settings: "encrypted:" + value)
    monkeypatch.setattr(codex, "_jwt_claims", lambda token: {"sub": "subject"})
    monkeypatch.setattr(
        codex.httpx,
        "post",
        lambda *args, **kwargs: httpx.Response(
            200, json={"access_token": "renewed-access", "refresh_token": "renewed-refresh"}
        ),
    )

    def catalog(info):
        assert info.access_token == "renewed-access"
        if available is None:
            raise codex.CodexConnectionError("catalog unavailable")
        return [codex.CodexModel(slug, slug) for slug in available]

    monkeypatch.setattr(codex, "_catalog_models", catalog)
    result = codex.complete_codex_oauth(state="state", code="code", settings=_settings())
    assert result.selected_model == expected
    assert result.encrypted_access_token == "encrypted:renewed-access"
    assert result.encrypted_refresh_token == "encrypted:renewed-refresh"
    assert result.active is True
    assert oauth.consumed_at is not None


def test_rejected_replacement_is_not_retried_again(monkeypatch):
    row = _model_resolution_db(monkeypatch)
    calls = []
    monkeypatch.setattr(
        codex, "_catalog_models", lambda info: [codex.CodexModel("account-first", "First")]
    )
    monkeypatch.setattr(
        codex.httpx,
        "stream",
        lambda *args, **kwargs: (
            calls.append(kwargs)
            or _StreamContext(httpx.Response(400, json={"error": {"code": "unsupported_model"}}))
        ),
    )
    with pytest.raises(codex.CodexConnectionError, match="HTTP 400"):
        codex._stream(_session(), "request")
    assert len(calls) == 2
    assert row.selected_model == "account-first"


@pytest.mark.parametrize("selected", ["", "valid"])
def test_session_resolves_only_when_no_model_is_saved(monkeypatch, selected):
    from datetime import UTC, datetime, timedelta

    row = SimpleNamespace(
        id=uuid.uuid4(),
        selected_model=selected,
        active=True,
        encrypted_access_token="token",
        account_id="account",
        email=None,
        plan_type=None,
        access_token_expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    monkeypatch.setattr(codex, "active_connection", lambda: row)
    monkeypatch.setattr(codex, "decrypt_secret", lambda value, settings: value)
    resolved = []

    def resolve(info):
        from dataclasses import replace

        resolved.append(info)
        return replace(info, model="catalog-first")

    monkeypatch.setattr(codex, "_resolve_model", resolve)
    assert codex.active_session(_settings()).model == (selected or "catalog-first")
    assert len(resolved) == (0 if selected else 1)
