import json
import uuid

import httpx

from katcha.config import Settings
from katcha.integrations import chatgpt


def _settings() -> Settings:
    return Settings(
        chatgpt_host_id="urn:uuid:11111111-1111-4111-8111-111111111111",
        chatgpt_oauth_redirect_uri="http://127.0.0.1:8765/auth/callback",
    )


def test_initial_authorization_uses_dynamic_registration_and_plan_scope() -> None:
    params = chatgpt.authorization_parameters(
        settings=_settings(),
        state="state",
        nonce="nonce",
        code_challenge="challenge",
    )

    assert params["client_id"] == "dynamic_agent_client"
    assert params["agent_name_hint"] == "Katcha"
    assert params["ext_agent_host_id"].startswith("urn:uuid:")
    assert params["redirect_uri"] == "http://127.0.0.1:8765/auth/callback"
    assert "chatgpt.tokens.use.direct" in params["scope"].split()
    assert params["resource"] == "https://api.openai.com/v1"


def test_returning_authorization_reuses_issued_client_and_hints() -> None:
    params = chatgpt.authorization_parameters(
        settings=_settings(),
        state="state",
        nonce="nonce",
        code_challenge="challenge",
        issued_client_id="oaiapp_saved",
        id_token_hint="id-token",
        login_hint="person@example.com",
    )

    assert params["client_id"] == "oaiapp_saved"
    assert "agent_name_hint" not in params
    assert params["id_token_hint"] == "id-token"
    assert params["login_hint"] == "person@example.com"


def _session() -> chatgpt.ChatGPTSession:
    return chatgpt.ChatGPTSession(
        connection_id=uuid.uuid4(),
        access_token="oauth-access-token",
        model="gpt-test",
        email="person@example.com",
        display_name="Person",
    )


class _StreamContext:
    def __init__(self, response: httpx.Response) -> None:
        self.response = response

    def __enter__(self) -> httpx.Response:
        return self.response

    def __exit__(self, *_args) -> bool:
        self.response.close()
        return False


def _sse_response(*events: dict[str, object], status_code: int = 200) -> httpx.Response:
    body = "".join(
        "data: " + json.dumps(event) + "\n\n"
        for event in events
    )
    return httpx.Response(
        status_code,
        content=body.encode(),
        request=httpx.Request("POST", "https://api.openai.com/v1/responses"),
    )


def test_chatgpt_plan_inference_uses_fresh_direct_stream(monkeypatch) -> None:
    captured = {}

    def stream(method, url, **kwargs):
        captured["method"] = method
        captured["url"] = url
        captured.update(kwargs)
        return _StreamContext(
            _sse_response(
                {
                    "type": "response.output_text.delta",
                    "delta": '{"answer":"hello"}',
                },
                {
                    "type": "response.completed",
                    "response": {
                        "usage": {"input_tokens": 12, "output_tokens": 5},
                    },
                },
            )
        )

    monkeypatch.setattr(chatgpt, "active_session", lambda settings=None: _session())
    monkeypatch.setattr(chatgpt.httpx, "stream", stream)

    result = chatgpt.invoke_json(
        prompt="hello",
        schema_name="test_schema",
        schema={"type": "object", "properties": {"answer": {"type": "string"}}},
    )

    assert result.text == '{"answer":"hello"}'
    assert result.model == "gpt-test"
    assert captured["method"] == "POST"
    assert captured["url"] == "https://api.openai.com/v1/responses"
    assert captured["json"]["store"] is False
    assert captured["json"]["stream"] is True
    assert captured["json"]["model"] == "gpt-test"
    assert captured["json"]["text"]["format"]["type"] == "json_schema"
    assert captured["headers"]["Authorization"].startswith("Bearer ")
    for unsupported in (
        "background",
        "conversation",
        "max_output_tokens",
        "metadata",
        "previous_response_id",
        "temperature",
        "top_p",
        "user",
    ):
        assert unsupported not in captured["json"]


def test_chatgpt_plan_stream_preserves_http_failure_detail(monkeypatch) -> None:
    response = httpx.Response(
        403,
        json={
            "error": {
                "code": "subscription_sharing_user_not_eligible",
                "message": "This account is not eligible for direct plan usage.",
            }
        },
        headers={"x-request-id": "req_fixture"},
        request=httpx.Request("POST", "https://api.openai.com/v1/responses"),
    )
    monkeypatch.setattr(chatgpt, "active_session", lambda settings=None: _session())
    monkeypatch.setattr(
        chatgpt.httpx,
        "stream",
        lambda *args, **kwargs: _StreamContext(response),
    )

    try:
        chatgpt.invoke_json(
            prompt="hello",
            schema_name="test_schema",
            schema={"type": "object", "properties": {"answer": {"type": "string"}}},
        )
    except chatgpt.ChatGPTConnectionError as exc:
        detail = str(exc)
    else:
        raise AssertionError("expected ChatGPTConnectionError")

    assert "HTTP 403" in detail
    assert "subscription_sharing_user_not_eligible" in detail
    assert "req_fixture" in detail
