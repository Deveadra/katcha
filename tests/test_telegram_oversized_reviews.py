"""Large Telegram review cards must not exceed Bot API upload limits."""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from katcha import telegram_worker
from katcha.config import Settings
from katcha.services import telegram_reviews
from katcha.services.telegram_reviews import (
    OversizedTelegramReview,
    TelegramReviewCard,
)


def _card(kind: str = "production") -> TelegramReviewCard:
    return TelegramReviewCard(
        session_id=uuid.uuid4(),
        source_kind=kind,
        source_id=uuid.uuid4(),
        callback_token="fixture-token",
        caption="Review the production render.",
        render_key="production/fixture/final.mp4",
        file_id=None,
        chat_id=123,
    )


def _prepare_queue(monkeypatch, card):
    row = SimpleNamespace(id=card.session_id)
    monkeypatch.setattr(telegram_worker, "operator_binding", lambda _settings: (123, 456))
    monkeypatch.setattr(telegram_worker, "ensure_review_sessions", lambda **kwargs: [])
    monkeypatch.setattr(telegram_worker, "pending_review_sessions", lambda **kwargs: [row])
    monkeypatch.setattr(telegram_worker, "review_card", lambda _id: card)
    return row


def test_render_above_limit_raises_typed_exception_without_downloading(monkeypatch) -> None:
    calls = []

    class FakeStore:
        def __init__(self, settings):
            pass

        def stat(self, key):
            return {"size_bytes": 69 * 1024 * 1024}

        def get_bytes(self, key):
            calls.append(key)
            return b"should-not-download"

    monkeypatch.setattr(telegram_reviews, "ObjectStore", FakeStore)
    with pytest.raises(OversizedTelegramReview, match="configured Bot API limit"):
        telegram_reviews.render_bytes(_card(), settings=Settings(_env_file=None))
    assert not calls


@pytest.mark.asyncio
async def test_oversized_production_uses_link_not_telegram_video(monkeypatch) -> None:
    card = _card()
    _prepare_queue(monkeypatch, card)
    settings = Settings(_env_file=None, telegram_enabled=True, telegram_chat_id=123)
    calls = {}

    def too_big(*args, **kwargs):
        raise OversizedTelegramReview("68.7 MB is above 50 MB")

    monkeypatch.setattr(telegram_worker, "render_bytes", too_big)
    monkeypatch.setattr(
        telegram_worker,
        "oversized_review_url",
        lambda *args, **kwargs: "https://media.example.com/review.mp4?signature=test",
    )
    monkeypatch.setattr(
        telegram_worker,
        "set_session_state",
        lambda sid, state, metadata=None: calls.update(
            {"mode": metadata["delivery_mode"], "intermediate": state}
        ),
    )
    monkeypatch.setattr(
        telegram_worker,
        "mark_sent",
        lambda sid, *, message_id, telegram_file_id: calls.update(
            {"sent_id": sid, "message_id": message_id, "file_id": telegram_file_id}
        ),
    )

    class Client:
        def send_message(self, chat_id, text, *, reply_markup):
            calls["chat_id"] = chat_id
            calls["text"] = text
            calls["markup"] = reply_markup
            return {"message_id": 71}

        def send_video(self, *args, **kwargs):
            raise AssertionError("Oversized media must never be uploaded")

    await telegram_worker._deliver_pending(Client(), settings)
    assert calls["sent_id"] == card.session_id
    assert calls["message_id"] == 71
    assert calls["file_id"] is None
    assert calls["mode"] == "oversized_review_link"
    assert calls["markup"]["inline_keyboard"][0][0]["url"].startswith("https://")
    assert "upload limit" in calls["text"]


@pytest.mark.asyncio
async def test_normal_size_review_still_uses_telegram_video(monkeypatch) -> None:
    card = _card()
    _prepare_queue(monkeypatch, card)
    settings = Settings(_env_file=None, telegram_enabled=True, telegram_chat_id=123)
    calls = {}

    monkeypatch.setattr(telegram_worker, "render_bytes", lambda *args, **kwargs: b"mp4")
    monkeypatch.setattr(
        telegram_worker,
        "oversized_review_url",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("Do not generate links for small videos")
        ),
    )
    monkeypatch.setattr(
        telegram_worker,
        "mark_sent",
        lambda sid, *, message_id, telegram_file_id: calls.update(
            {"id": sid, "message_id": message_id, "file_id": telegram_file_id}
        ),
    )

    class Client:
        def send_message(self, *args, **kwargs):
            raise AssertionError("Normal video should be uploaded")

        def send_video(self, chat_id, **kwargs):
            assert kwargs["video"] == b"mp4"
            return {"message_id": 12, "video": {"file_id": "telegram-fixture"}}

    await telegram_worker._deliver_pending(Client(), settings)
    assert calls["file_id"] == "telegram-fixture"


@pytest.mark.asyncio
async def test_oversized_without_accessible_endpoint_does_not_send_dead_card(monkeypatch):
    card = _card()
    _prepare_queue(monkeypatch, card)
    settings = Settings(_env_file=None, telegram_enabled=True, telegram_chat_id=123)
    calls = []

    def too_big(*args, **kwargs):
        raise OversizedTelegramReview("Too large")

    monkeypatch.setattr(telegram_worker, "render_bytes", too_big)
    monkeypatch.setattr(telegram_worker, "oversized_review_url", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        telegram_worker,
        "mark_failed",
        lambda sid, err: calls.append((sid, err)),
    )

    class Client:
        def send_message(self, *args, **kwargs):
            raise AssertionError("No unplayable cards allowed")

        def send_video(self, *args, **kwargs):
            raise AssertionError("No over-limit uploads allowed")

    await telegram_worker._deliver_pending(Client(), settings)
    assert len(calls) == 1
    assert calls[0][0] == card.session_id
    assert "HTTPS review media endpoint" in calls[0][1]


@pytest.mark.asyncio
async def test_refresh_oversized_text_card_mints_new_signed_url(monkeypatch) -> None:
    card = _card()
    row = SimpleNamespace(
        id=card.session_id,
        chat_id=123,
        message_id=80,
        session_metadata={"delivery_mode": "oversized_review_link"},
    )
    settings = Settings(_env_file=None)
    monkeypatch.setattr(telegram_worker, "review_card", lambda sid: card)
    monkeypatch.setattr(
        telegram_worker,
        "oversized_review_url",
        lambda *args, **kwargs: "https://media.example.com/new.mp4?signature=rotated",
    )
    calls = {}

    class Client:
        def edit_text(self, chat_id, message_id, *, text, reply_markup):
            calls.update(
                {"chat_id": chat_id, "message_id": message_id, "markup": reply_markup}
            )

        def edit_caption(self, *args, **kwargs):
            raise AssertionError("Link cards have text, not video captions")

    result = await telegram_worker._refresh(Client(), row, settings)
    assert result == "Secure review link refreshed."
    assert calls["message_id"] == 80
    assert "rotated" in calls["markup"]["inline_keyboard"][0][0]["url"]


@pytest.mark.parametrize("endpoint", ["http://media.example.com", "http://minio:9000"])
def test_insecure_oversized_review_endpoint_is_refused(monkeypatch, endpoint: str) -> None:
    settings = Settings(
        _env_file=None,
        s3_endpoint_url=endpoint,
        telegram_review_storage_endpoint_url=None,
    )
    assert telegram_reviews.oversized_review_url(_card(), settings=settings) is None


def test_https_oversized_link_uses_existing_presigned_storage(monkeypatch) -> None:
    settings = Settings(
        _env_file=None,
        s3_endpoint_url="https://objects.example.com",
        telegram_review_link_ttl_seconds=900,
    )
    calls = {}

    class FakeStore:
        def __init__(self, current):
            assert current is settings

        def presigned_get_url(self, key, *, expires_seconds, filename, endpoint_url):
            calls.update(
                {"key": key, "ttl": expires_seconds, "filename": filename}
            )
            return "https://objects.example.com/signed?signature=fixture"

    monkeypatch.setattr(telegram_reviews, "ObjectStore", FakeStore)
    assert telegram_reviews.oversized_review_url(_card(), settings=settings).startswith(
        "https://objects.example.com/"
    )
    assert calls["ttl"] == 900
    assert calls["filename"].startswith("katcha-review-")
