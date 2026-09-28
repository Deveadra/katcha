from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import UniqueConstraint

from katcha import telegram_worker
from katcha.api.main import app
from katcha.config import Settings
from katcha.domain import ReviewDecision
from katcha.editorial.episode_generator import build_ranked_episode_prompt
from katcha.editorial.generator import build_script_prompt
from katcha.editorial.personas import get_persona
from katcha.longform.editor import _editor_prompt
from katcha.services.telegram_reviews import (
    TelegramReviewCard,
    keyboard,
    longform_review_links_ready,
    longform_review_url,
)
from katcha.telegram_models import TelegramReviewSession


def test_telegram_callback_payloads_fit_bot_api_limit() -> None:
    markup = keyboard("compact-token")
    buttons = [
        button
        for row in markup["inline_keyboard"]
        for button in row
    ]

    callbacks = [button["callback_data"] for button in buttons]
    assert {value.split(":")[1] for value in callbacks} == {"a", "e", "b", "r", "f"}
    assert all(len(value.encode("utf-8")) <= 64 for value in callbacks)





def test_longform_review_keyboard_uses_url_without_expanding_callbacks() -> None:
    review_url = "https://media.example.com/review.mp4?token=fixture"
    markup = keyboard("compact-token", review_url=review_url)

    assert markup["inline_keyboard"][0] == [
        {"text": "▶ Watch full video", "url": review_url}
    ]
    callbacks = [
        button["callback_data"]
        for row in markup["inline_keyboard"][1:]
        for button in row
    ]
    assert all(len(value.encode("utf-8")) <= 64 for value in callbacks)


def test_local_minio_does_not_emit_dead_longform_link() -> None:
    settings = Settings(
        _env_file=None,
        s3_endpoint_url="http://minio:9000",
    )
    card = TelegramReviewCard(
        session_id=uuid.uuid4(),
        source_kind="compilation",
        source_id=uuid.uuid4(),
        callback_token="compact-token",
        caption="Fixture",
        render_key="compilation/test/final.mp4",
        file_id=None,
        chat_id=123,
    )

    assert longform_review_links_ready(settings) is False
    assert longform_review_url(card, settings=settings) is None


def test_external_minio_endpoint_mints_expiring_longform_link() -> None:
    settings = Settings(
        _env_file=None,
        s3_endpoint_url="http://minio:9000",
        telegram_review_storage_endpoint_url="https://media.example.com",
        telegram_review_link_ttl_seconds=900,
    )
    card = TelegramReviewCard(
        session_id=uuid.uuid4(),
        source_kind="compilation",
        source_id=uuid.uuid4(),
        callback_token="compact-token",
        caption="Fixture",
        render_key="compilation/test/final.mp4",
        file_id=None,
        chat_id=123,
    )

    url = longform_review_url(card, settings=settings)

    assert longform_review_links_ready(settings) is True
    assert url is not None
    assert url.startswith("https://media.example.com/")
    assert "X-Amz-Signature=" in url


def test_telegram_review_source_is_unique_per_chat() -> None:
    unique_columns = {
        tuple(column.name for column in constraint.columns)
        for constraint in TelegramReviewSession.__table__.constraints
        if isinstance(constraint, UniqueConstraint)
    }

    assert ("source_kind", "source_id", "chat_id") in unique_columns


def test_blank_telegram_settings_are_optional() -> None:
    settings = Settings(
        _env_file=None,
        telegram_bot_token="",
        telegram_chat_id="",
        telegram_allowed_user_id="",
        telegram_pairing_code="",
        telegram_review_storage_endpoint_url="",
    )

    assert settings.telegram_bot_token is None
    assert settings.telegram_chat_id is None
    assert settings.telegram_allowed_user_id is None
    assert settings.telegram_pairing_code is None
    assert settings.telegram_review_storage_endpoint_url is None


def test_configured_chat_and_user_both_gate_control() -> None:
    settings = Settings(
        _env_file=None,
        telegram_chat_id=123,
        telegram_allowed_user_id=456,
    )

    assert telegram_worker._authorized(settings, chat_id=123, user_id=456)
    assert not telegram_worker._authorized(settings, chat_id=123, user_id=999)
    assert not telegram_worker._authorized(settings, chat_id=999, user_id=456)


def test_one_off_operator_feedback_is_injected_into_regeneration_prompt() -> None:
    prompt = build_ranked_episode_prompt(
        get_persona("youth_host"),
        premise="Rank the best fixture moments",
        plan_snapshot={
            "ordered_items": [],
            "operator_feedback": [
                {
                    "actor": "telegram:123",
                    "note": "Cut the slow setup and make the narration less cheesy.",
                    "regenerate_from": "script",
                }
            ],
        },
        items=[
            {
                "position": 1,
                "clip_id": "clip-1",
                "role": "payoff",
                "editorial_signals": {},
                "analysis_snapshot": {},
            }
        ],
        prompt_version="test-v1",
    )

    assert "One-off regeneration feedback" in prompt
    assert "Cut the slow setup and make the narration less cheesy." in prompt
    assert "Do not silently turn one-off feedback into permanent channel policy." in prompt


@pytest.mark.asyncio
async def test_telegram_approval_uses_authoritative_short_episode_review(
    monkeypatch,
) -> None:
    calls: dict[str, object] = {}

    def fake_review(source_id, *, decision, actor, note):
        calls["source_id"] = source_id
        calls["decision"] = decision
        calls["actor"] = actor
        calls["note"] = note

    monkeypatch.setattr(telegram_worker, "review_short_episode", fake_review)
    monkeypatch.setattr(
        telegram_worker,
        "advance_render_automation",
        lambda *_args: SimpleNamespace(
            action="approved",
            publication_id=None,
            publication_workflow_id=None,
        ),
    )
    monkeypatch.setattr(telegram_worker, "set_session_state", lambda *_a, **_k: None)
    monkeypatch.setattr(telegram_worker, "_record_action", lambda *_a, **_k: None)

    class Client:
        def clear_buttons(self, _chat_id, _message_id):
            calls["buttons_cleared"] = True

    source_id = uuid.uuid4()
    row = SimpleNamespace(
        id=uuid.uuid4(),
        source_kind="short_episode",
        source_id=source_id,
        chat_id=123,
        message_id=99,
    )

    result = await telegram_worker._approve(Client(), row, "telegram:456")

    assert result == "Approved. Katcha recorded the decision."
    assert calls["source_id"] == source_id
    assert calls["decision"] == ReviewDecision.APPROVE
    assert calls["actor"] == "telegram:456"
    assert calls["buttons_cleared"] is True



def test_telegram_control_routes_are_mounted_without_secret_fields() -> None:
    paths = app.openapi()["paths"]
    assert "/v1/integrations/telegram/status" in paths
    assert "/v1/integrations/telegram/test" in paths

    status_schema = app.openapi()["components"]["schemas"]["TelegramStatusResponse"]
    fields = set(status_schema["properties"])
    assert "bot_configured" in fields
    assert "paired" in fields
    assert "longform_review_links_ready" in fields
    assert "longform_review_link_ttl_seconds" in fields
    assert "telegram_bot_token" not in fields
    assert "pairing_code" not in fields


def test_single_clip_regeneration_prompt_honors_operator_feedback() -> None:
    prompt = build_script_prompt(
        get_persona("youth_host"),
        {
            "duration_seconds": 20,
            "operator_feedback": [
                {
                    "actor": "telegram:123",
                    "note": "Open faster and make the CTA less generic.",
                    "regenerate_from": "script",
                }
            ],
        },
        prompt_version="test-v1",
    )

    assert "Open faster and make the CTA less generic." in prompt
    assert "Do not convert it into permanent channel policy." in prompt


def test_longform_regeneration_prompt_honors_operator_feedback() -> None:
    prompt = _editor_prompt(
        theme="Fixture compilation",
        target_duration_seconds=480,
        persona=get_persona("youth_host"),
        candidates=[],
        prompt_version="test-v1",
        operator_feedback=[
            {
                "actor": "telegram:123",
                "note": "The middle drags. Tighten it and preserve the strongest payoff.",
                "regenerate_from": "plan",
            }
        ],
    )

    assert "The middle drags. Tighten it and preserve the strongest payoff." in prompt
    assert "Do not convert one-off feedback into permanent channel policy." in prompt
