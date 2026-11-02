"""Offline notification outbox tests; no Telegram calls or provider access."""

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from katcha import db
from katcha.acquisition_job_models import AcquisitionChallenge, AcquisitionJob
from katcha.config import Settings
from katcha.services import acquisition_notifications as notifications
from katcha.telegram_models import TelegramBotCursor


@pytest.fixture
def outbox(monkeypatch, tmp_path):
    db.load_model_metadata()
    engine = create_engine(f"sqlite:///{tmp_path / 'outbox.sqlite'}")
    db.Base.metadata.create_all(engine, tables=[
        AcquisitionJob.__table__, AcquisitionChallenge.__table__, TelegramBotCursor.__table__,
    ])
    monkeypatch.setattr(db, "SessionLocal", sessionmaker(bind=engine, expire_on_commit=False))
    channel = uuid.uuid4()
    now = datetime.now(UTC)
    with db.session_scope() as session:
        job = AcquisitionJob(
            receipt_id=uuid.uuid4(), channel_profile_id=channel,
            status="waiting_human", generation=1, max_bytes=100,
        )
        session.add(job)
        session.flush()
        challenge = AcquisitionChallenge(
            job_id=job.id, channel_profile_id=channel, generation=1,
            expires_at=now + timedelta(minutes=30),
        )
        session.add(challenge)
        session.flush()
    settings = Settings(
        _env_file=None, telegram_enabled=True, telegram_bot_token="fixture-token",
        telegram_chat_id=123, telegram_allowed_user_id=456,
        telegram_acquisition_app_origin="https://katcha.example",
        telegram_acquisition_channel_ids=(channel,),
    )
    calls = []

    def send_message(*args, **kwargs):
        calls.append((args, kwargs))
        return {"message_id": 17}

    yield SimpleNamespace(
        settings=settings, job=job, challenge=challenge, calls=calls,
        client=SimpleNamespace(send_message=send_message),
    )
    engine.dispose()


def current(outbox):
    with db.session_scope() as session:
        return session.get(AcquisitionChallenge, outbox.challenge.id)


def test_delivers_once_with_authenticated_app_link_and_no_source_details(outbox):
    result = notifications.deliver_pending_acquisition_notifications(outbox.client, outbox.settings)
    assert result["sent"] == 1
    assert current(outbox).notification_state == "sent"
    assert current(outbox).notification_message_id == 17
    notifications.deliver_pending_acquisition_notifications(outbox.client, outbox.settings)
    assert len(outbox.calls) == 1
    args, kwargs = outbox.calls[0]
    assert args[0] == 123
    assert "Your request is saved" in args[1]
    assert kwargs["reply_markup"] == {"inline_keyboard": [[{
        "text": "Open verification",
        "url": "https://katcha.example/acquisition-recovery?"
        f"channel={outbox.job.channel_profile_id}&job={outbox.job.id}",
    }]]}
    assert "fixture-token" not in repr(outbox.calls)
    assert "callback_data" not in repr(outbox.calls)


@pytest.mark.parametrize("change", [
    {"telegram_enabled": False}, {"telegram_bot_token": None},
    {"telegram_acquisition_app_origin": None}, {"telegram_acquisition_channel_ids": ()},
    {"telegram_allowed_user_id": None},
])
def test_missing_admission_never_sends_and_recovers_after_configuration(outbox, change):
    settings = outbox.settings.model_copy(update=change)
    notifications.deliver_pending_acquisition_notifications(outbox.client, settings)
    assert outbox.calls == []
    assert current(outbox).notification_state == "not_configured"
    notifications.deliver_pending_acquisition_notifications(outbox.client, outbox.settings)
    assert len(outbox.calls) == 1


def test_ambiguous_send_is_not_retried_or_logged_as_provider_error(outbox):
    def fail(*args, **kwargs):
        outbox.calls.append(True)
        raise TimeoutError("secret-cookie-or-bot-token")
    outbox.client.send_message = fail
    notifications.deliver_pending_acquisition_notifications(outbox.client, outbox.settings)
    notifications.deliver_pending_acquisition_notifications(outbox.client, outbox.settings)
    assert len(outbox.calls) == 1
    assert current(outbox).notification_state == "unknown"
    assert current(outbox).notification_message_id is None


def test_crash_after_claim_does_not_duplicate_send(outbox):
    with db.session_scope() as session:
        row = session.get(AcquisitionChallenge, outbox.challenge.id)
        row.notification_state = "sending"
        row.notification_updated_at = datetime.now(UTC) - timedelta(minutes=3)
    notifications.deliver_pending_acquisition_notifications(outbox.client, outbox.settings)
    assert outbox.calls == []
    assert current(outbox).notification_state == "unknown"


@pytest.mark.parametrize("change", ["expired", "closed", "cancelled", "generation", "channel"])
def test_stale_or_cancelled_challenge_never_sends(outbox, change):
    with db.session_scope() as session:
        row = session.get(AcquisitionChallenge, outbox.challenge.id)
        job = session.get(AcquisitionJob, outbox.job.id)
        if change == "expired":
            row.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        elif change == "closed":
            row.state = "closed"
        elif change == "cancelled":
            job.status = "cancelled"
        elif change == "generation":
            job.generation = 2
        else:
            row.channel_profile_id = uuid.uuid4()
    notifications.deliver_pending_acquisition_notifications(outbox.client, outbox.settings)
    assert outbox.calls == []


def test_claim_is_committed_before_send_and_other_scanner_cannot_duplicate(outbox):
    def concurrent(*args, **kwargs):
        assert current(outbox).notification_state == "sending"
        nested = SimpleNamespace(send_message=lambda *a, **k: pytest.fail("Duplicate send"))
        notifications.deliver_pending_acquisition_notifications(nested, outbox.settings)
        return {"message_id": 18}
    outbox.client.send_message = concurrent
    notifications.deliver_pending_acquisition_notifications(outbox.client, outbox.settings)
    assert current(outbox).notification_message_id == 18


@pytest.mark.parametrize("origin", [
    "http://katcha.example", "https://user:password@katcha.example",
    "https://katcha.example/path", "https://katcha.example?token=secret",
    "https://katcha.example#fragment", "https://katcha.example:8443", "javascript:alert(1)",
])
def test_notification_origin_rejects_credentials_paths_and_non_https(origin):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, telegram_acquisition_app_origin=origin)


def test_origin_normalizes_trailing_slash():
    assert Settings(
        _env_file=None, telegram_acquisition_app_origin=" https://katcha.example/ "
    ).telegram_acquisition_app_origin == "https://katcha.example"


def test_unconfigured_old_rows_do_not_starve_an_admitted_channel(outbox):
    with db.session_scope() as session:
        original = session.get(AcquisitionChallenge, outbox.challenge.id)
        original.created_at = datetime.now(UTC) + timedelta(seconds=1)
        for _ in range(105):
            channel = uuid.uuid4()
            job = AcquisitionJob(
                receipt_id=uuid.uuid4(), channel_profile_id=channel,
                status="waiting_human", generation=1, max_bytes=100,
            )
            session.add(job)
            session.flush()
            session.add(AcquisitionChallenge(
                job_id=job.id, channel_profile_id=channel, generation=1,
                expires_at=datetime.now(UTC) + timedelta(minutes=30),
            ))
    first = notifications.deliver_pending_acquisition_notifications(outbox.client, outbox.settings)
    assert first["not_configured"] == 100
    assert first["sent"] == 0
    second = notifications.deliver_pending_acquisition_notifications(outbox.client, outbox.settings)
    assert second["sent"] == 1
    assert len(outbox.calls) == 1


@pytest.mark.parametrize("reply", [{}, {"message_id": True}, {"message_id": -1}])
def test_malformed_response_preserves_uncertainty_without_duplicate(outbox, reply):
    def malformed(*args, **kwargs):
        outbox.calls.append(True)
        return reply
    outbox.client.send_message = malformed
    notifications.deliver_pending_acquisition_notifications(outbox.client, outbox.settings)
    notifications.deliver_pending_acquisition_notifications(outbox.client, outbox.settings)
    assert current(outbox).notification_state == "unknown"
    assert len(outbox.calls) == 1
