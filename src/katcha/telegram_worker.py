from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any

from sqlalchemy import func, select

from katcha.config import Settings, get_settings
from katcha.db import session_scope
from katcha.domain import ReviewDecision
from katcha.integrations.telegram import TelegramAPIError, TelegramBotClient
from katcha.models import DomainEvent
from katcha.orchestration.client import (
    start_longform_workflow,
    start_production_workflow,
    start_publication_workflow,
    start_short_episode_editorial_workflow,
)
from katcha.services.compilations import (
    register_compilation_regeneration,
    review_compilation,
)
from katcha.services.productions import register_regeneration, review_production
from katcha.services.render_automation import advance_render_automation
from katcha.services.short_episode_reviews import (
    register_short_episode_regeneration,
    review_short_episode,
)
from katcha.services.telegram_reviews import (
    backlogged_sessions,
    ensure_review_sessions,
    feedback_session,
    keyboard,
    longform_review_url,
    mark_failed,
    mark_sent,
    operator_binding,
    pair_operator,
    pending_review_sessions,
    render_bytes,
    review_card,
    session_for_callback,
    set_session_state,
)
from katcha.telegram_models import TelegramBotCursor, TelegramReviewSession

LOGGER = logging.getLogger("katcha.telegram")
_CURSOR_KEY = "operator-bot"


def _authorized(
    settings: Settings,
    *,
    chat_id: int | None,
    user_id: int | None,
) -> bool:
    allowed_chat, allowed_user = operator_binding(settings)
    if allowed_chat is None or chat_id != allowed_chat:
        return False
    return allowed_user is None or user_id == allowed_user


def _cursor() -> int | None:
    with session_scope() as session:
        row = session.get(TelegramBotCursor, _CURSOR_KEY)
        if row is None or row.last_update_id <= 0:
            return None
        return row.last_update_id + 1


def _save_cursor(update_id: int) -> None:
    with session_scope() as session:
        row = session.get(TelegramBotCursor, _CURSOR_KEY)
        if row is None:
            row = TelegramBotCursor(key=_CURSOR_KEY, last_update_id=update_id)
            session.add(row)
        elif update_id > row.last_update_id:
            row.last_update_id = update_id


def _record_action(
    session_id: uuid.UUID,
    *,
    action: str,
    actor: str,
    metadata: dict[str, object] | None = None,
) -> None:
    with session_scope() as session:
        row = session.get(TelegramReviewSession, session_id)
        if row is None:
            return
        session.add(
            DomainEvent(
                aggregate_type="telegram_review",
                aggregate_id=str(session_id),
                event_type=f"telegram.review_{action}",
                payload={
                    "source_kind": row.source_kind,
                    "source_id": str(row.source_id),
                    "channel_profile_id": (
                        str(row.channel_profile_id)
                        if row.channel_profile_id
                        else None
                    ),
                    "actor": actor,
                    **dict(metadata or {}),
                },
            )
        )


async def _deliver_pending(
    client: TelegramBotClient,
    settings: Settings,
) -> None:
    chat_id, _ = operator_binding(settings)
    ensure_review_sessions(chat_id=chat_id, settings=settings)
    if chat_id is None:
        return
    for row in pending_review_sessions(chat_id=chat_id, limit=5):
        try:
            card = review_card(row.id)
            if card.source_kind == "compilation":
                review_url = longform_review_url(card, settings=settings)
                if review_url:
                    text = (
                        card.caption
                        + "\n\n▶ Open the full video with the secure review link below. "
                        + "Refresh this card if the link expires."
                    )
                else:
                    text = (
                        card.caption
                        + "\n\n⚠ Full-video review link is not externally reachable yet. "
                        + "Configure Telegram's external review media endpoint, then tap Refresh."
                    )
                message = await asyncio.to_thread(
                    client.send_message,
                    card.chat_id,
                    text,
                    reply_markup=keyboard(
                        card.callback_token,
                        review_url=review_url,
                    ),
                )
                file_id = None
                set_session_state(
                    row.id,
                    "queued",
                    metadata={
                        "delivery_mode": "review_link",
                        "review_link_available": review_url is not None,
                    },
                )
            else:
                video: bytes | str
                if card.file_id:
                    video = card.file_id
                else:
                    video = await asyncio.to_thread(
                        render_bytes,
                        card,
                        settings=settings,
                    )
                message = await asyncio.to_thread(
                    client.send_video,
                    card.chat_id,
                    video=video,
                    filename=f"katcha-{card.source_id}.mp4",
                    caption=card.caption,
                    reply_markup=keyboard(card.callback_token),
                )
                telegram_video = dict(message.get("video") or {})
                file_id = str(telegram_video.get("file_id") or "") or None
            mark_sent(
                row.id,
                message_id=int(message["message_id"]),
                telegram_file_id=file_id,
            )
        except (TelegramAPIError, RuntimeError, ValueError) as exc:
            LOGGER.exception("Telegram review delivery failed for %s", row.id)
            mark_failed(row.id, str(exc))


async def _approve(
    client: TelegramBotClient,
    row: TelegramReviewSession,
    actor: str,
) -> str:
    automation = None
    if row.source_kind == "short_episode":
        review_short_episode(
            row.source_id,
            decision=ReviewDecision.APPROVE,
            actor=actor,
            note="Approved from Telegram",
        )
        automation = advance_render_automation("short_episode", row.source_id)
    elif row.source_kind == "production":
        review_production(
            row.source_id,
            decision=ReviewDecision.APPROVE,
            actor=actor,
            note="Approved from Telegram",
        )
        automation = advance_render_automation("production", row.source_id)
    elif row.source_kind == "compilation":
        review_compilation(
            row.source_id,
            decision=ReviewDecision.APPROVE,
            actor=actor,
            note="Approved from Telegram",
        )
    else:
        raise ValueError("Telegram approval source type is unsupported")

    if (
        automation is not None
        and automation.publication_id
        and automation.publication_workflow_id
    ):
        await start_publication_workflow(
            automation.publication_id,
            automation.publication_workflow_id,
        )
    metadata = {
        "automation_action": automation.action if automation is not None else "approved"
    }
    set_session_state(row.id, "approved", metadata=metadata)
    if row.message_id is not None:
        await asyncio.to_thread(client.clear_buttons, row.chat_id, row.message_id)
    _record_action(
        row.id,
        action="approved",
        actor=actor,
        metadata=metadata,
    )
    if automation is not None and automation.action == "publication_queued":
        return "Approved. YouTube publication is queued."
    return "Approved. Katcha recorded the decision."

async def _reject(
    client: TelegramBotClient,
    row: TelegramReviewSession,
    actor: str,
) -> str:
    if row.source_kind == "short_episode":
        review_short_episode(
            row.source_id,
            decision=ReviewDecision.REJECT,
            actor=actor,
            note="Rejected from Telegram",
        )
    elif row.source_kind == "production":
        review_production(
            row.source_id,
            decision=ReviewDecision.REJECT,
            actor=actor,
            note="Rejected from Telegram",
        )
    elif row.source_kind == "compilation":
        review_compilation(
            row.source_id,
            decision=ReviewDecision.REJECT,
            actor=actor,
            note="Rejected from Telegram",
        )
    else:
        raise ValueError("Telegram rejection source type is unsupported")
    set_session_state(row.id, "rejected")
    if row.message_id is not None:
        await asyncio.to_thread(client.clear_buttons, row.chat_id, row.message_id)
    _record_action(row.id, action="rejected", actor=actor)
    return "Rejected. This generation will not move forward."

async def _backlog(
    client: TelegramBotClient,
    row: TelegramReviewSession,
    actor: str,
) -> str:
    set_session_state(row.id, "backlogged")
    if row.message_id is not None:
        await asyncio.to_thread(client.clear_buttons, row.chat_id, row.message_id)
    _record_action(row.id, action="backlogged", actor=actor)
    return "Moved to backlog. Send /backlog to reopen the oldest deferred review."


async def _request_edit_feedback(
    client: TelegramBotClient,
    row: TelegramReviewSession,
    actor: str,
) -> str:
    prompt = await asyncio.to_thread(
        client.send_message,
        row.chat_id,
        (
            "Tell Katcha exactly what you want changed. Reply to this message with "
            "specific notes about pacing, narration, clip choices, timing, audio, branding, "
            "or anything else you noticed."
        ),
        reply_markup={
            "force_reply": True,
            "input_field_placeholder": "What should Katcha change?",
            "selective": True,
        },
    )
    set_session_state(
        row.id,
        "awaiting_edit_feedback",
        prompt_message_id=int(prompt["message_id"]),
    )
    _record_action(row.id, action="edit_requested", actor=actor)
    return "Reply with your edit notes."


async def _refresh(
    client: TelegramBotClient,
    row: TelegramReviewSession,
    settings: Settings,
) -> str:
    if row.message_id is None:
        set_session_state(row.id, "queued")
        return "Review re-queued."
    card = review_card(row.id)
    if card.source_kind == "compilation":
        review_url = longform_review_url(card, settings=settings)
        if review_url:
            text = (
                card.caption
                + "\n\n▶ Open the full video with the secure review link below. "
                + "This refresh minted a new expiring link."
            )
        else:
            text = (
                card.caption
                + "\n\n⚠ Full-video review link is not externally reachable yet. "
                + "Configure Telegram's external review media endpoint, then tap Refresh."
            )
        await asyncio.to_thread(
            client.edit_text,
            row.chat_id,
            row.message_id,
            text=text,
            reply_markup=keyboard(
                card.callback_token,
                review_url=review_url,
            ),
        )
    else:
        await asyncio.to_thread(
            client.edit_caption,
            row.chat_id,
            row.message_id,
            caption=card.caption,
            reply_markup=keyboard(card.callback_token),
        )
    return "Review refreshed from current Katcha state."


async def _handle_callback(
    client: TelegramBotClient,
    settings: Settings,
    callback: dict[str, Any],
) -> None:
    callback_id = str(callback.get("id") or "")
    data = str(callback.get("data") or "")
    message = dict(callback.get("message") or {})
    chat = dict(message.get("chat") or {})
    sender = dict(callback.get("from") or {})
    chat_id = int(chat["id"]) if chat.get("id") is not None else None
    user_id = int(sender["id"]) if sender.get("id") is not None else None

    if not _authorized(settings, chat_id=chat_id, user_id=user_id):
        if callback_id:
            await asyncio.to_thread(
                client.answer_callback,
                callback_id,
                text="This Katcha control is not authorized for you.",
                show_alert=True,
            )
        return

    parts = data.split(":")
    if len(parts) != 3 or parts[0] != "k":
        if callback_id:
            await asyncio.to_thread(
                client.answer_callback,
                callback_id,
                text="Unknown or expired Katcha action.",
                show_alert=True,
            )
        return

    action, token = parts[1], parts[2]
    try:
        row = session_for_callback(token)
        if row.chat_id != chat_id:
            raise ValueError("review action does not belong to this chat")
        if row.state not in {
            "sent",
            "awaiting_edit_feedback",
            "backlogged",
        }:
            raise ValueError(f"review is already {row.state}")
        actor = f"telegram:{user_id}"
        if action == "a":
            result = await _approve(client, row, actor)
        elif action == "r":
            result = await _reject(client, row, actor)
        elif action == "b":
            result = await _backlog(client, row, actor)
        elif action == "e":
            result = await _request_edit_feedback(client, row, actor)
        elif action == "f":
            result = await _refresh(client, row, settings)
        else:
            raise ValueError("unknown Katcha review action")
        if callback_id:
            await asyncio.to_thread(client.answer_callback, callback_id, text=result)
    except (TelegramAPIError, RuntimeError, ValueError) as exc:
        LOGGER.exception("Telegram callback failed")
        if callback_id:
            await asyncio.to_thread(
                client.answer_callback,
                callback_id,
                text=str(exc)[:180],
                show_alert=True,
            )


async def _handle_feedback(
    client: TelegramBotClient,
    settings: Settings,
    message: dict[str, Any],
) -> bool:
    chat = dict(message.get("chat") or {})
    sender = dict(message.get("from") or {})
    chat_id = int(chat["id"]) if chat.get("id") is not None else None
    user_id = int(sender["id"]) if sender.get("id") is not None else None
    if not _authorized(settings, chat_id=chat_id, user_id=user_id):
        return False

    reply = dict(message.get("reply_to_message") or {})
    reply_id = reply.get("message_id")
    text = str(message.get("text") or "").strip()
    if reply_id is None or not text or chat_id is None:
        return False
    row = feedback_session(
        chat_id=chat_id,
        reply_to_message_id=int(reply_id),
    )
    if row is None:
        return False
    if not settings.ai_enabled:
        await asyncio.to_thread(
            client.send_message,
            chat_id,
            "Katcha received the edit notes, but AI execution is disabled. "
            "Enable AI execution before asking for a regenerated edit.",
        )
        return True

    try:
        actor = f"telegram:{user_id}"
        if row.source_kind == "short_episode":
            child = register_short_episode_regeneration(
                row.source_id,
                stage="script",
                note=text[:2000],
                actor=actor,
            )
            workflow_id = f"{child.workflow_id}-editorial-script"
            await start_short_episode_editorial_workflow(
                str(child.id),
                workflow_id,
                start_stage="script",
            )
        elif row.source_kind == "production":
            child = register_regeneration(
                row.source_id,
                stage="script",
                note=text[:2000],
                actor=actor,
            )
            workflow_id = child.workflow_id
            await start_production_workflow(
                str(child.id),
                workflow_id,
                start_stage="script",
            )
        elif row.source_kind == "compilation":
            child = register_compilation_regeneration(
                row.source_id,
                stage="plan",
                note=text[:2000],
                actor=actor,
            )
            workflow_id = child.workflow_id
            await start_longform_workflow(
                str(child.id),
                workflow_id,
                start_stage=child.regenerate_from or "plan",
            )
        else:
            raise ValueError("Telegram edit source type is unsupported")

        set_session_state(
            row.id,
            "regenerating",
            metadata={
                "child_source_id": str(child.id),
                "feedback": text[:2000],
            },
        )
        if row.message_id is not None:
            await asyncio.to_thread(client.clear_buttons, row.chat_id, row.message_id)
        _record_action(
            row.id,
            action="regeneration_queued",
            actor=actor,
            metadata={
                "child_source_id": str(child.id),
                "feedback": text[:2000],
            },
        )
        rebuild_stage = (
            "plan" if row.source_kind == "compilation" else "script"
        )
        await asyncio.to_thread(
            client.send_message,
            chat_id,
            (
                "Edit accepted. Katcha created generation "
                f"{child.generation} and is rebuilding it from the {rebuild_stage} stage. "
                "The revised playable video will return here for review."
            ),
        )
    except (RuntimeError, ValueError) as exc:
        LOGGER.exception("Telegram edit feedback failed")
        await asyncio.to_thread(
            client.send_message,
            chat_id,
            f"Katcha could not queue that edit: {str(exc)[:600]}",
        )
    return True


async def _handle_command(
    client: TelegramBotClient,
    settings: Settings,
    message: dict[str, Any],
) -> bool:
    chat = dict(message.get("chat") or {})
    sender = dict(message.get("from") or {})
    chat_id = int(chat["id"]) if chat.get("id") is not None else None
    user_id = int(sender["id"]) if sender.get("id") is not None else None
    text = str(message.get("text") or "").strip()
    if not text.startswith("/"):
        return False
    if chat_id is None or user_id is None:
        return True

    parts = text.split()
    command = parts[0].split("@")[0].lower()
    if command == "/start" and not _authorized(
        settings,
        chat_id=chat_id,
        user_id=user_id,
    ):
        pairing_code = parts[1] if len(parts) > 1 else ""
        if pair_operator(
            chat_id=chat_id,
            user_id=user_id,
            pairing_code=pairing_code,
            settings=settings,
        ):
            await asyncio.to_thread(
                client.send_message,
                chat_id,
                (
                    "Katcha is paired to this Telegram account. "
                    "Rendered videos that need review will now arrive here automatically."
                ),
            )
        else:
            await asyncio.to_thread(
                client.send_message,
                chat_id,
                (
                    "This Katcha bot is not paired to your account. "
                    "Use /start followed by the pairing code shown in the Katcha launcher."
                ),
            )
        return True

    if not _authorized(settings, chat_id=chat_id, user_id=user_id):
        return True

    if command in {"/start", "/help"}:
        await asyncio.to_thread(
            client.send_message,
            chat_id,
            (
                "Katcha Telegram control is connected.\n\n"
                "Rendered videos that need review will appear here automatically. "
                "Use the buttons to approve, reject, defer, refresh, or request an edit.\n"
                "/backlog — reopen the oldest deferred review\n"
                "/status — show Telegram review queue status"
            ),
        )
        return True

    if command == "/backlog":
        rows = backlogged_sessions(chat_id=chat_id, limit=1)
        if not rows:
            await asyncio.to_thread(
                client.send_message,
                chat_id,
                "Telegram review backlog is empty.",
            )
            return True
        set_session_state(rows[0].id, "queued")
        await asyncio.to_thread(
            client.send_message,
            chat_id,
            "Reopened the oldest deferred review. It will appear here next.",
        )
        return True

    if command == "/status":
        with session_scope() as session:
            counts = dict(
                session.execute(
                    select(
                        TelegramReviewSession.state,
                        func.count(TelegramReviewSession.id),
                    )
                    .where(TelegramReviewSession.chat_id == chat_id)
                    .group_by(TelegramReviewSession.state)
                ).all()
            )
        summary = ", ".join(
            f"{state}: {count}" for state, count in sorted(counts.items())
        ) or "no review sessions yet"
        await asyncio.to_thread(
            client.send_message,
            chat_id,
            "Katcha Telegram review status — " + summary,
        )
        return True
    return True


async def _handle_update(
    client: TelegramBotClient,
    settings: Settings,
    update: dict[str, Any],
) -> None:
    callback = update.get("callback_query")
    if isinstance(callback, dict):
        await _handle_callback(client, settings, callback)
        return

    message = update.get("message")
    if not isinstance(message, dict):
        return
    if await _handle_feedback(client, settings, message):
        return
    await _handle_command(client, settings, message)


async def main() -> None:
    settings = get_settings()
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    if not settings.telegram_enabled:
        LOGGER.info(
            "Telegram worker is dormant; set KATCHA_TELEGRAM_ENABLED=true to enable it"
        )
        while True:
            await asyncio.sleep(3600)

    if settings.telegram_bot_token is None:
        LOGGER.error(
            "Telegram is enabled but KATCHA_TELEGRAM_BOT_TOKEN is missing; "
            "worker remains dormant"
        )
        while True:
            await asyncio.sleep(300)

    client: TelegramBotClient | None = None
    while client is None:
        try:
            candidate = TelegramBotClient(settings)
            identity = await asyncio.to_thread(candidate.get_me)
            await asyncio.to_thread(candidate.delete_webhook)
            LOGGER.info(
                "Telegram worker connected as @%s",
                identity.get("username") or identity.get("id"),
            )
            client = candidate
        except TelegramAPIError:
            LOGGER.exception("Telegram bot authentication failed; retrying")
            await asyncio.sleep(30)

    while True:
        try:
            await _deliver_pending(client, settings)
            updates = await asyncio.to_thread(
                client.get_updates,
                offset=_cursor(),
                timeout_seconds=min(
                    settings.telegram_poll_timeout_seconds,
                    settings.telegram_review_scan_seconds,
                ),
            )
            for update in updates:
                update_id = int(update.get("update_id") or 0)
                try:
                    await _handle_update(client, settings, update)
                except Exception:
                    LOGGER.exception("Unhandled Telegram update failure: %s", update_id)
                finally:
                    if update_id:
                        _save_cursor(update_id)
        except TelegramAPIError:
            LOGGER.exception("Telegram API loop failed")
            await asyncio.sleep(5)
        except Exception:
            LOGGER.exception("Telegram worker loop failed")
            await asyncio.sleep(5)


if __name__ == "__main__":
    asyncio.run(main())
