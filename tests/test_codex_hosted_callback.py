from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from katcha.api import chatgpt, codex, control_auth
from katcha.config import Settings


def test_completion_is_operator_authenticated_not_public(monkeypatch):
    monkeypatch.setattr(control_auth, "get_settings", lambda: Settings(
        _env_file=None, control_api_token="fixture-operator-token"
    ))
    request = Request({"type": "http", "method": "POST",
                       "path": "/v1/integrations/codex/oauth/complete",
                       "headers": [], "query_string": b""})
    with pytest.raises(HTTPException) as error:
        control_auth.require_control_token(request, None)
    assert error.value.status_code == 401
    request.state.control_actor = "fixture"
    request.state.control_principal_name = "fixture-principal"
    request.state.control_scopes = {"ai:command"}
    request.state.control_allowed_channel_ids = {"*"}
    with pytest.raises(HTTPException) as error:
        control_auth._require_named_principal_route_access(request)
    assert error.value.status_code == 403


def test_start_supplies_browser_state_without_changing_redirect(monkeypatch):
    monkeypatch.setattr(codex, "begin_codex_oauth", lambda:
                        "https://auth.openai.com/oauth/authorize?state=browser-state")
    response = codex.codex_oauth_start()
    assert response.state == "browser-state"
    assert response.authorization_url.startswith("https://auth.openai.com/")


def test_authenticated_completion_returns_no_credentials(monkeypatch):
    captured = {}
    def complete(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(encrypted_access_token="must-not-return")
    monkeypatch.setattr(codex, "complete_codex_callback_url", complete)
    request = codex.OAuthCompleteRequest(
        callback_url="http://localhost:1457/auth/callback?code=code&state=state",
        expected_state="browser-initiated-state-123",
    )
    assert codex.codex_oauth_complete(request) == {"ok": True, "connected": True}
    assert captured == request.model_dump()


@pytest.mark.parametrize("outcome", ["connected", "error", "state_error"])
def test_callback_returns_to_current_origin_settings(monkeypatch, outcome):
    monkeypatch.setattr(chatgpt, "is_codex_oauth_state", lambda state: True)
    monkeypatch.setattr(chatgpt, "complete_codex_oauth", lambda **kwargs: None)
    def failed(state):
        if outcome == "state_error":
            raise chatgpt.CodexConnectionError("expired")
    monkeypatch.setattr(chatgpt, "consume_failed_codex_oauth", failed)
    response = chatgpt.chatgpt_oauth_callback(
        state="state", code="code" if outcome == "connected" else None,
        error=None if outcome == "connected" else "denied",
    )
    assert response.headers["location"] == f"/settings?codex={outcome}"


@pytest.mark.parametrize("error", [None, "access_denied"])
def test_foreign_callback_keeps_address_for_hosted_completion(monkeypatch, error):
    monkeypatch.setattr(chatgpt, "is_codex_oauth_state", lambda state: False)
    monkeypatch.setattr(chatgpt, "is_chatgpt_oauth_state", lambda state: False)
    def must_not_exchange(**kwargs):
        pytest.fail("A foreign sign-in must not exchange or consume credentials")
    monkeypatch.setattr(chatgpt, "complete_chatgpt_oauth", must_not_exchange)
    monkeypatch.setattr(chatgpt, "consume_failed_oauth", must_not_exchange)
    response = chatgpt.chatgpt_oauth_callback(
        state="foreign-state", code="private-code", error=error,
    )
    assert response.status_code == 200
    assert "location" not in response.headers
    assert "Sign-in return address" in response.body.decode()
    assert "private-code" not in response.body.decode()
    assert "foreign-state" not in response.body.decode()
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert "default-src 'none'" in response.headers["content-security-policy"]


def test_local_chatgpt_callback_still_exchanges_known_state(monkeypatch):
    monkeypatch.setattr(chatgpt, "is_codex_oauth_state", lambda state: False)
    monkeypatch.setattr(chatgpt, "is_chatgpt_oauth_state", lambda state: True)
    received = {}
    monkeypatch.setattr(chatgpt, "complete_chatgpt_oauth", lambda **kw: received.update(kw))
    response = chatgpt.chatgpt_oauth_callback(state="local-state", code="local-code")
    assert response.headers["location"] == "/settings?chatgpt=connected"
    assert received["state"] == "local-state"
    assert received["code"] == "local-code"
