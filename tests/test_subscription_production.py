"""Provider contracts use synthetic replies, never a live account."""

from decimal import Decimal
from types import SimpleNamespace

import pytest
from pydantic import BaseModel

from katcha.ai import subscription
from katcha.config import Settings
from katcha.domain import AITask


class Answer(BaseModel):
    answer: str


@pytest.fixture
def live_settings(monkeypatch):
    monkeypatch.setattr(subscription.codex, "connection_status", lambda: {"connected": True})
    return Settings(
        ai_enabled=True,
        ai_execution_mode="live",
        codex_enabled=True,
        credential_encryption_key="fixture",
        openai_api_key=None,
        gemini_api_key=None,
    )


def generate(settings, **kwargs):
    return subscription.generate_subscription_json(
        prompt="fixture prompt",
        schema=Answer,
        task=AITask.SHORT_SCRIPT,
        reference_type="fixture",
        reference_id="fixture",
        settings=settings,
        **kwargs,
    )


def test_subscription_needs_no_paid_api_budget_and_records_zero_cost(monkeypatch, live_settings):
    usage = []
    captured = []

    def invoke(**kwargs):
        captured.append(kwargs)
        return SimpleNamespace(
            text='{"answer":"works"}', model="fixture", input_tokens=2, output_tokens=3
        )

    monkeypatch.setattr(subscription.codex, "invoke_json", invoke)
    monkeypatch.setattr(subscription, "record_usage", lambda **kwargs: usage.append(kwargs))
    result = generate(live_settings, image_bytes=b"synthetic contact sheet")
    assert result.value.answer == "works"
    assert result.target.provider == "codex"
    assert captured[0]["image_bytes"] == b"synthetic contact sheet"
    assert usage[0]["cost_usd"] == Decimal("0")
    assert usage[0]["metadata"]["subscription_backed"] is True


def test_subscription_failure_keeps_provider_error(monkeypatch, live_settings):
    def fail(**kwargs):
        raise subscription.codex.CodexConnectionError("authentication expired")

    monkeypatch.setattr(subscription.codex, "invoke_json", fail)
    with pytest.raises(RuntimeError, match="authentication expired"):
        generate(live_settings)
    live_settings.openai_api_key = "fixture-api-key"
    assert generate(live_settings) is None  # caller can use configured API fallback


def test_fixture_mode_never_invokes_subscription(monkeypatch, live_settings):
    live_settings.ai_execution_mode = "fixture"
    monkeypatch.setattr(
        subscription.codex, "invoke_json", lambda **kwargs: pytest.fail("live call")
    )
    assert generate(live_settings) is None
