"""Provider readiness must reflect real generation, not saved credential metadata."""

import sys
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from katcha.api import chatgpt as api
from katcha.config import Settings
from katcha.integrations import chatgpt, codex


def test_openai_provider_probe_requires_real_inference(monkeypatch):
    monkeypatch.setattr(
        api, "get_settings", lambda: Settings(_env_file=None, openai_api_key="test-only-key")
    )
    calls = []

    class FakeOpenAI:
        def __init__(self, **kwargs):
            calls.append(("client", kwargs))

        @property
        def responses(self):
            return self

        def create(self, **kwargs):
            calls.append(("inference", kwargs))
            return SimpleNamespace(output_text="KATCHA_OPENAI_CONNECTED")

    monkeypatch.setitem(sys.modules, "openai", SimpleNamespace(OpenAI=FakeOpenAI))
    result = api.test_api_provider("openai")
    assert result["ok"] is True
    assert "Live OpenAI inference verified" in result["detail"]
    assert calls[1][1]["store"] is False
    assert calls[1][1]["max_output_tokens"] <= 64
    assert calls[1][1]["model"] == "gpt-5.6-luna"


def test_openai_provider_probe_rejects_wrong_response(monkeypatch):
    monkeypatch.setattr(
        api, "get_settings", lambda: Settings(_env_file=None, openai_api_key="test-only-key")
    )

    class FakeOpenAI:
        def __init__(self, **kwargs):
            pass

        @property
        def responses(self):
            return self

        def create(self, **kwargs):
            return SimpleNamespace(output_text="")

    monkeypatch.setitem(sys.modules, "openai", SimpleNamespace(OpenAI=FakeOpenAI))
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        api.test_api_provider("openai")
    assert exc.value.status_code == 503
    assert "did not complete the expected inference" in exc.value.detail
    assert "test-only-key" not in exc.value.detail


@pytest.mark.parametrize("age_minutes,expected", [(-10, True), (10, False)])
def test_codex_saved_status_reports_refresh_due_not_live_verified(
    monkeypatch, age_minutes, expected
):
    monkeypatch.setattr(
        codex,
        "active_connection",
        lambda: SimpleNamespace(
            id=uuid.uuid4(), email=None, account_id=None,
            plan_type=None, selected_model="gpt-5.5",
            access_token_expires_at=datetime.now(UTC) + timedelta(minutes=age_minutes),
        ),
    )
    status = codex.connection_status()
    assert status["connected"] is True
    assert status["refresh_due"] is expected
    assert "verified" not in status


@pytest.mark.parametrize("age_minutes,expected", [(-10, True), (10, False)])
def test_chatgpt_saved_status_reports_refresh_due(monkeypatch, age_minutes, expected):
    monkeypatch.setattr(
        chatgpt,
        "active_connection",
        lambda: SimpleNamespace(
            id=uuid.uuid4(), scopes=[chatgpt.PLAN_SCOPE],
            email=None, display_name=None, selected_model="gpt-5.5",
            access_token_expires_at=datetime.now(UTC) + timedelta(minutes=age_minutes),
        ),
    )
    status = chatgpt.connection_status()
    assert status["connected"] is True
    assert status["refresh_due"] is expected
