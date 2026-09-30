import sys
from types import SimpleNamespace

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


def test_chatgpt_plan_inference_uses_preview_safe_responses_shape(monkeypatch) -> None:
    captured = {}

    class Stream:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def __iter__(self):
            yield SimpleNamespace(
                type="response.output_text.delta",
                delta='{"answer":"hello"}',
            )
            yield SimpleNamespace(
                type="response.completed",
                response=SimpleNamespace(
                    usage=SimpleNamespace(input_tokens=12, output_tokens=5)
                ),
            )

    def create(**kwargs):
        captured.update(kwargs)
        return Stream()

    def client(**kwargs):
        assert kwargs["api_key"] == "oauth-access-token"
        assert kwargs["base_url"] == "https://api.openai.com/v1"
        assert kwargs["max_retries"] == 0
        return SimpleNamespace(responses=SimpleNamespace(create=create))

    monkeypatch.setattr(
        chatgpt,
        "active_session",
        lambda settings=None: chatgpt.ChatGPTSession(
            connection_id=__import__("uuid").uuid4(),
            access_token="oauth-access-token",
            model="gpt-test",
            email="person@example.com",
            display_name="Person",
        ),
    )
    monkeypatch.setitem(sys.modules, "openai", SimpleNamespace(OpenAI=client))

    result = chatgpt.invoke_json(
        prompt="hello",
        schema_name="test_schema",
        schema={"type": "object", "properties": {"answer": {"type": "string"}}},
    )

    assert result.text == '{"answer":"hello"}'
    assert result.model == "gpt-test"
    assert captured["store"] is False
    assert captured["stream"] is True
    assert captured["model"] == "gpt-test"
    assert captured["text"]["format"]["type"] == "json_schema"
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
        assert unsupported not in captured
