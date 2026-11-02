"""At-most-once Telegram escalation for durable acquisition challenges.

Telegram offers no idempotency key for sendMessage. A timeout or crash after
claiming a notification therefore becomes ``unknown``, not a duplicate send.
The authenticated Katcha page remains the source of truth and the only control.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode

from sqlalchemy import select, update

from katcha.acquisition_job_models import AcquisitionChallenge, AcquisitionJob
from katcha.config import Settings
from katcha.db import session_scope
from katcha.integrations.telegram import TelegramBotClient
from katcha.services.telegram_reviews import operator_binding


def _now():
    return datetime.now(UTC)


def notification_configuration_error(settings: Settings, channel_id) -> str | None:
    """Credential-free readiness, also usable by the operator recovery page."""
    if not settings.telegram_enabled or not settings.telegram_bot_token:
        return "telegram_not_enabled"
    if not settings.telegram_acquisition_app_origin:
        return "app_origin_not_configured"
    if channel_id not in settings.telegram_acquisition_channel_ids:
        return "channel_not_admitted"
    chat, user = operator_binding(settings)
    if chat is None or user is None:
        return "operator_not_paired"
    return None


def _eligible(now):
    return (
        AcquisitionChallenge.state == "waiting",
        AcquisitionChallenge.expires_at > now,
        AcquisitionChallenge.job_id.in_(
            select(AcquisitionJob.id).where(
                AcquisitionJob.status == "waiting_human",
                AcquisitionJob.generation == AcquisitionChallenge.generation,
                AcquisitionJob.channel_profile_id == AcquisitionChallenge.channel_profile_id,
            )
        ),
    )


def deliver_pending_acquisition_notifications(
    client: TelegramBotClient, settings: Settings, *, limit: int = 1
) -> dict[str, int]:
    """One bounded worker pass; no raw API errors, source URLs or secrets are saved."""
    now = _now()
    counts = {"sent": 0, "unknown": 0, "not_configured": 0}
    with session_scope() as session:
        # A process may die after Telegram accepted a request but before its ID was saved.
        session.execute(
            update(AcquisitionChallenge)
            .where(
                AcquisitionChallenge.notification_state == "sending",
                AcquisitionChallenge.notification_updated_at < now - timedelta(minutes=2),
            )
            .values(notification_state="unknown", notification_updated_at=now)
        )
        session.execute(
            update(AcquisitionChallenge)
            .where(
                AcquisitionChallenge.notification_state.in_(["pending", "not_configured"]),
                AcquisitionChallenge.expires_at <= now,
            )
            .values(notification_state="expired", notification_updated_at=now)
        )
        identities = list(session.scalars(
            select(AcquisitionChallenge.id)
            .where(
                *_eligible(now),
                AcquisitionChallenge.notification_state.in_(["pending", "not_configured"]),
            )
            .order_by(
                # Unconfigured channels must not permanently occupy the first page.
                AcquisitionChallenge.notification_updated_at.asc().nullsfirst(),
                AcquisitionChallenge.created_at,
                AcquisitionChallenge.id,
            )
            .limit(100)
        ))

    attempts = 0
    for identity in identities:
        with session_scope() as session:
            row = session.get(AcquisitionChallenge, identity)
            if notification_configuration_error(settings, row.channel_profile_id):
                session.execute(
                    update(AcquisitionChallenge)
                    .where(
                        AcquisitionChallenge.id == identity,
                        *_eligible(_now()),
                        AcquisitionChallenge.notification_state.in_(["pending", "not_configured"]),
                    )
                    .values(notification_state="not_configured", notification_updated_at=_now())
                )
                counts["not_configured"] += 1
                continue
            if attempts >= max(1, min(limit, 5)):
                break
            chat_id, _ = operator_binding(settings)
            changed = session.scalar(
                update(AcquisitionChallenge)
                .where(
                    AcquisitionChallenge.id == identity,
                    *_eligible(_now()),
                    AcquisitionChallenge.notification_state.in_(["pending", "not_configured"]),
                )
                .values(notification_state="sending", notification_updated_at=_now())
                .returning(AcquisitionChallenge.id)
            )
            if changed is None:
                continue
            link = settings.telegram_acquisition_app_origin + "/acquisition-recovery?" + urlencode(
                {"channel": str(row.channel_profile_id), "job": str(row.job_id)}
            )
        # The send claim is committed before crossing the external side-effect boundary.
        attempts += 1
        try:
            result = client.send_message(
                chat_id,
                "Katcha needs your help to continue a clip download. "
                "Open verification, complete the site's check in that browser, "
                "then choose Check access and continue. Your request is saved. "
                "Signing in in a separate browser will not update this download session.",
                reply_markup={"inline_keyboard": [[{"text": "Open verification", "url": link}]]},
            )
            message_id = result.get("message_id")
            # A malformed response is not proof that the side effect did not happen.
            if isinstance(message_id, bool) or not isinstance(message_id, int) or message_id <= 0:
                raise ValueError("Telegram response has no message identity")
        except Exception:
            state, message_id = "unknown", None
        else:
            state = "sent"
        with session_scope() as session:
            session.execute(
                update(AcquisitionChallenge)
                .where(
                    AcquisitionChallenge.id == identity,
                    AcquisitionChallenge.notification_state == "sending",
                )
                .values(
                    notification_state=state,
                    notification_message_id=message_id,
                    notification_updated_at=_now(),
                )
            )
        counts[state] += 1
    return counts
