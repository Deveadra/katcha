"""Hosted credentials are shared and encrypted, with environment fallback preserved."""

import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from katcha import db
from katcha.api import ai_settings as api
from katcha.api.main import app
from katcha.config import Settings
from katcha.models import DomainEvent
from katcha.provider_setting_models import ApplicationAISetting
from katcha.services import application_ai_settings as service


@pytest.fixture
def hosted(monkeypatch):
    db.load_model_metadata()
    engine = create_engine(
        "sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    db.Base.metadata.create_all(engine)
    monkeypatch.setattr(db, "SessionLocal", sessionmaker(bind=engine, expire_on_commit=False))
    settings = Settings(
        _env_file=None,
        env="test",
        control_api_token=None,
        credential_encryption_key="MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA=",
        gemini_api_key="environment-gemini",
        allow_paid_openai_fallback=False,
    )
    monkeypatch.setattr(api, "get_settings", lambda: settings)
    monkeypatch.setattr("katcha.api.control_auth.get_settings", lambda: settings)
    yield settings
    engine.dispose()


def test_hosted_save_encrypted_shared_and_never_returned(hosted):
    with TestClient(app) as client:
        before = client.get("/v1/integrations/ai/settings")
        assert before.status_code == 200
        assert before.json()["settings"]["KATCHA_GEMINI_API_KEY"] is True
        result = client.post(
            "/v1/integrations/ai/settings",
            json={
                "KATCHA_GEMINI_API_KEY": "new-test-secret",
                "KATCHA_AI_EXECUTION_MODE": "live",
                "KATCHA_CONVERSATION_PROVIDER": "codex",
            },
        )
        assert result.status_code == 200
        assert "new-test-secret" not in result.text
        assert "environment-gemini" not in result.text
    # An independent worker resolution sees the committed values, no restart.
    resolved = service.effective_settings(hosted)
    assert resolved.gemini_api_key == "new-test-secret"
    assert resolved.conversation_provider == "codex"
    assert resolved.ai_enabled is True
    assert resolved.allow_paid_openai_fallback is False
    assert hosted.gemini_api_key == "environment-gemini"
    with db.session_scope() as session:
        row = session.get(ApplicationAISetting, "KATCHA_GEMINI_API_KEY")
        assert row.value != "new-test-secret"
        event = session.scalars(select(DomainEvent)).first()
        assert "new-test-secret" not in json.dumps(event.payload)
    service.save_settings({"KATCHA_GEMINI_API_KEY": ""}, hosted, actor="test")
    assert service.effective_settings(hosted).gemini_api_key == "new-test-secret"


def test_invalid_batch_does_not_partially_save_or_echo_secret(hosted):
    with TestClient(app) as client:
        result = client.post(
            "/v1/integrations/ai/settings",
            json={
                "KATCHA_OPENAI_API_KEY": "private-test-secret",
                "KATCHA_AGENT_PROVIDER": "invalid",
            },
        )
        assert result.status_code == 400
        assert "private-test-secret" not in result.text
    assert service.effective_settings(hosted).openai_api_key == hosted.openai_api_key
    with db.session_scope() as session:
        assert session.get(ApplicationAISetting, "KATCHA_OPENAI_API_KEY") is None


def test_shared_settings_require_global_operator(hosted, monkeypatch):
    from pydantic import SecretStr

    settings = hosted.model_copy(update={"control_api_token": SecretStr("operator-test-token")})
    monkeypatch.setattr("katcha.api.control_auth.get_settings", lambda: settings)
    with TestClient(app) as client:
        assert client.get("/v1/integrations/ai/settings").status_code == 401
        assert client.post("/v1/integrations/ai/settings", json={}).status_code == 401


@pytest.mark.parametrize(
    "scopes,channels", [(["ai:read"], ["*"]), (["*"], ["00000000-0000-0000-0000-000000000001"])]
)
def test_channel_scoped_principal_cannot_read_or_change_global_settings(
    hosted, monkeypatch, scopes, channels
):
    settings = Settings(
        _env_file=None,
        env="test",
        control_principals=[
            {
                "name": "scoped",
                "token": "scoped-test-token-0001",
                "scopes": scopes,
                "channel_profile_ids": channels,
            }
        ],
    )
    monkeypatch.setattr("katcha.api.control_auth.get_settings", lambda: settings)
    with TestClient(app) as client:
        headers = {"Authorization": "Bearer scoped-test-token-0001"}
        assert client.get("/v1/integrations/ai/settings", headers=headers).status_code == 403
        assert (
            client.post("/v1/integrations/ai/settings", json={}, headers=headers).status_code == 403
        )
        assert (
            client.post(
                "/v1/integrations/codex/oauth/complete",
                headers=headers,
                json={
                    "callback_url": "http://localhost:1457/auth/callback?state=fixture-state-123456&code=code",
                    "expected_state": "fixture-state-123456",
                },
            ).status_code
            == 403
        )
