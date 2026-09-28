from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from sqlalchemy import func, select

from katcha.config import get_settings
from katcha.db import session_scope
from katcha.integrations.telegram import TelegramAPIError, TelegramBotClient
from katcha.services.telegram_reviews import (
    longform_review_links_ready,
    operator_binding,
)
from katcha.telegram_models import TelegramReviewSession

router = APIRouter(prefix="/v1/integrations/telegram", tags=["telegram"])


class TelegramStatusResponse(BaseModel):
    enabled: bool
    bot_configured: bool
    paired: bool
    pairing_required: bool
    allowed_user_bound: bool
    longform_review_links_ready: bool
    longform_review_link_ttl_seconds: int
    review_counts: dict[str, int]
    last_delivery_error: str | None


class TelegramTestResponse(BaseModel):
    status: str
    chat_id: int


def _review_status(chat_id: int | None) -> tuple[dict[str, int], str | None]:
    if chat_id is None:
        return {}, None
    with session_scope() as session:
        rows = session.execute(
            select(
                TelegramReviewSession.state,
                func.count(TelegramReviewSession.id),
            )
            .where(TelegramReviewSession.chat_id == chat_id)
            .group_by(TelegramReviewSession.state)
        ).all()
        latest_failed = session.scalar(
            select(TelegramReviewSession)
            .where(
                TelegramReviewSession.chat_id == chat_id,
                TelegramReviewSession.last_error.is_not(None),
            )
            .order_by(TelegramReviewSession.updated_at.desc())
            .limit(1)
        )
        return (
            {str(state): int(count) for state, count in rows},
            latest_failed.last_error if latest_failed is not None else None,
        )


@router.get("/status", response_model=TelegramStatusResponse)
def telegram_status() -> TelegramStatusResponse:
    settings = get_settings()
    chat_id, user_id = operator_binding(settings)
    counts, last_error = _review_status(chat_id)
    bot_configured = settings.telegram_bot_token is not None
    paired = chat_id is not None
    return TelegramStatusResponse(
        enabled=settings.telegram_enabled,
        bot_configured=bot_configured,
        paired=paired,
        pairing_required=bool(
            settings.telegram_enabled and bot_configured and not paired
        ),
        allowed_user_bound=user_id is not None,
        longform_review_links_ready=longform_review_links_ready(settings),
        longform_review_link_ttl_seconds=settings.telegram_review_link_ttl_seconds,
        review_counts=counts,
        last_delivery_error=last_error,
    )


@router.post("/test", response_model=TelegramTestResponse)
def send_telegram_test() -> TelegramTestResponse:
    settings = get_settings()
    if not settings.telegram_enabled:
        raise HTTPException(status_code=409, detail="Telegram integration is disabled")
    if settings.telegram_bot_token is None:
        raise HTTPException(status_code=409, detail="Telegram bot token is not configured")
    chat_id, _ = operator_binding(settings)
    if chat_id is None:
        raise HTTPException(
            status_code=409,
            detail="Telegram operator is not paired yet",
        )
    try:
        client = TelegramBotClient(settings)
        client.send_message(
            chat_id,
            (
                "Katcha Telegram control is connected. "
                "Short videos arrive as playable cards; long-form reviews use secure links."
            ),
        )
    except TelegramAPIError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return TelegramTestResponse(status="sent", chat_id=chat_id)
