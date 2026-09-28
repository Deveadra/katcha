from __future__ import annotations

import re
import secrets
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select

from katcha.config import Settings, get_settings
from katcha.db import session_scope
from katcha.integrations.storage import ObjectStore
from katcha.intelligence_models import ChannelProfile
from katcha.models import DomainEvent
from katcha.publishing_models import Publication
from katcha.services.channel_growth import channel_growth_context
from katcha.services.channel_profiles import active_strategy
from katcha.short_episode_models import (
    ShortEpisode,
    ShortEpisodeAsset,
    ShortEpisodeScript,
)
from katcha.telegram_models import TelegramReviewSession

_CATEGORY_NAMES = {
    "1": "Film & Animation",
    "10": "Music",
    "20": "Gaming",
    "22": "People & Blogs",
    "24": "Entertainment",
    "25": "News & Politics",
    "26": "Howto & Style",
    "27": "Education",
    "28": "Science & Technology",
}
_ACTIVE_STATES = {"queued", "sent", "awaiting_edit_feedback", "backlogged"}
_REVIEWABLE_EPISODE_STATES = {"rendered", "render_review"}


@dataclass(frozen=True, slots=True)
class TelegramReviewCard:
    session_id: uuid.UUID
    source_id: uuid.UUID
    callback_token: str
    caption: str
    render_key: str
    file_id: str | None
    chat_id: int


def _callback_token() -> str:
    return secrets.token_urlsafe(9)[:16]


def _publication_defaults(session: object, profile: ChannelProfile) -> dict[str, object]:
    strategy = active_strategy(session, profile)
    routing = dict(strategy.routing_policy or {})
    defaults = routing.get("publication_defaults")
    return dict(defaults) if isinstance(defaults, dict) else {}


def _episode_publication(
    session: object,
    episode_id: uuid.UUID,
) -> Publication | None:
    return session.scalar(
        select(Publication)
        .where(Publication.short_episode_id == episode_id)
        .order_by(Publication.created_at.desc())
        .limit(1)
    )


def _selected_script(
    session: object,
    episode: ShortEpisode,
) -> ShortEpisodeScript | None:
    if episode.selected_script_id is None:
        return None
    return session.get(ShortEpisodeScript, episode.selected_script_id)


def _render_asset(
    session: object,
    episode_id: uuid.UUID,
) -> ShortEpisodeAsset | None:
    return session.scalar(
        select(ShortEpisodeAsset)
        .where(
            ShortEpisodeAsset.short_episode_id == episode_id,
            ShortEpisodeAsset.kind == "render",
        )
        .order_by(
            ShortEpisodeAsset.generation.desc(),
            ShortEpisodeAsset.created_at.desc(),
        )
        .limit(1)
    )


def _hashtags(tags: list[str]) -> str:
    values: list[str] = []
    for raw in tags[:10]:
        cleaned = re.sub(r"[^A-Za-z0-9_]", "", str(raw).replace(" ", ""))
        if cleaned:
            values.append("#" + cleaned.lstrip("#"))
    return " ".join(values) or "—"


def _truncate(value: str, limit: int) -> str:
    text = " ".join(value.split())
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"


def _goal_line(channel_profile_id: uuid.UUID) -> str:
    try:
        growth = channel_growth_context(channel_profile_id)
    except (RuntimeError, ValueError):
        return "Goal: channel growth"
    ai = dict(growth.get("ai") or {})
    goals = dict(growth.get("goals") or {})
    objective = str(goals.get("objective") or "channel_growth").replace("_", " ")
    priorities = list(ai.get("priority_metrics") or [])
    priority = (
        str(priorities[0]).replace("_", " ")
        if priorities
        else str(ai.get("stage") or "growth").replace("_", " ")
    )
    return f"Goal: {objective} · Focus: {priority}"


def _caption(
    episode: ShortEpisode,
    script: ShortEpisodeScript | None,
    publication: Publication | None,
    defaults: dict[str, object],
) -> str:
    payload = dict(script.script_payload or {}) if script else {}
    title = (
        publication.title
        if publication is not None
        else str(payload.get("title_angle") or episode.premise)
    )
    description = (
        publication.description
        if publication is not None
        else str(defaults.get("description") or "")
    )
    tags = (
        list(publication.tags or [])
        if publication is not None
        else list(defaults.get("tags") or [])
    )
    category_id = (
        publication.category_id
        if publication is not None
        else str(defaults.get("category_id") or get_settings().youtube_default_category_id)
    )
    category = _CATEGORY_NAMES.get(category_id, f"YouTube category {category_id}")
    cta = str(
        payload.get("interaction_prompt")
        or payload.get("closing_line")
        or (episode.render_manifest or {}).get("end_card", {}).get("prompt")
        or "—"
    )
    youtube_url = (
        f"https://youtu.be/{publication.youtube_video_id}"
        if publication is not None and publication.youtube_video_id
        else "Not uploaded yet"
    )
    lines = [
        f"READY FOR REVIEW · generation {episode.generation}",
        "",
        f"Title: {_truncate(str(title), 120)}",
        f"Description: {_truncate(str(description), 180) if description else '—'}",
        f"Category: {category}",
        f"CTA: {_truncate(cta, 140)}",
        f"Hashtags: {_hashtags([str(tag) for tag in tags])}",
        f"YouTube: {youtube_url}",
        "",
        _goal_line(episode.channel_profile_id),
        f"Format: {episode.format_key} · Brand: {episode.brand_key}",
    ]
    return _truncate("\n".join(lines), 1024)


def keyboard(callback_token: str) -> dict[str, Any]:
    def button(text: str, action: str) -> dict[str, str]:
        return {
            "text": text,
            "callback_data": f"k:{action}:{callback_token}",
        }

    return {
        "inline_keyboard": [
            [
                button("✅ Approve", "a"),
                button("✏️ Send back", "e"),
            ],
            [
                button("📥 Backlog", "b"),
                button("❌ Reject", "r"),
                button("🔄 Refresh", "f"),
            ],
        ]
    }


def ensure_review_sessions(
    *,
    settings: Settings | None = None,
    limit: int = 20,
) -> list[TelegramReviewSession]:
    settings = settings or get_settings()
    if not settings.telegram_enabled or settings.telegram_chat_id is None:
        return []
    created: list[TelegramReviewSession] = []
    with session_scope() as session:
        episodes = list(
            session.scalars(
                select(ShortEpisode)
                .where(ShortEpisode.status.in_(_REVIEWABLE_EPISODE_STATES))
                .order_by(ShortEpisode.created_at)
                .limit(limit)
            )
        )
        for episode in episodes:
            asset = _render_asset(session, episode.id)
            if asset is None:
                continue
            existing = session.scalar(
                select(TelegramReviewSession).where(
                    TelegramReviewSession.source_kind == "short_episode",
                    TelegramReviewSession.source_id == episode.id,
                    TelegramReviewSession.chat_id == settings.telegram_chat_id,
                )
            )
            if existing is not None:
                continue
            row = TelegramReviewSession(
                source_kind="short_episode",
                source_id=episode.id,
                channel_profile_id=episode.channel_profile_id,
                callback_token=_callback_token(),
                chat_id=settings.telegram_chat_id,
                state="queued",
                session_metadata={"render_asset_id": str(asset.id)},
            )
            session.add(row)
            session.flush()
            session.refresh(row)
            session.expunge(row)
            created.append(row)
    return created


def pending_review_sessions(limit: int = 10) -> list[TelegramReviewSession]:
    with session_scope() as session:
        rows = list(
            session.scalars(
                select(TelegramReviewSession)
                .where(TelegramReviewSession.state == "queued")
                .order_by(TelegramReviewSession.created_at)
                .limit(limit)
            )
        )
        for row in rows:
            session.expunge(row)
        return rows


def review_card(session_id: uuid.UUID) -> TelegramReviewCard:
    with session_scope() as session:
        row = session.get(TelegramReviewSession, session_id)
        if row is None:
            raise ValueError(f"Telegram review session not found: {session_id}")
        if row.source_kind != "short_episode":
            raise ValueError("Telegram review source kind is not implemented yet")
        episode = session.get(ShortEpisode, row.source_id)
        if episode is None:
            raise ValueError("Telegram review episode no longer exists")
        profile = session.get(ChannelProfile, episode.channel_profile_id)
        if profile is None:
            raise ValueError("Telegram review channel profile no longer exists")
        asset = _render_asset(session, episode.id)
        if asset is None:
            raise ValueError("Telegram review episode has no rendered media")
        publication = _episode_publication(session, episode.id)
        script = _selected_script(session, episode)
        defaults = _publication_defaults(session, profile)
        return TelegramReviewCard(
            session_id=row.id,
            source_id=episode.id,
            callback_token=row.callback_token,
            caption=_caption(episode, script, publication, defaults),
            render_key=asset.storage_key,
            file_id=row.telegram_file_id,
            chat_id=row.chat_id,
        )


def mark_sent(
    session_id: uuid.UUID,
    *,
    message_id: int,
    telegram_file_id: str | None,
) -> None:
    with session_scope() as session:
        row = session.get(TelegramReviewSession, session_id)
        if row is None:
            raise ValueError("Telegram review session disappeared")
        row.message_id = message_id
        row.telegram_file_id = telegram_file_id or row.telegram_file_id
        row.state = "sent"
        row.last_error = None
        session.add(
            DomainEvent(
                aggregate_type="telegram_review",
                aggregate_id=str(row.id),
                event_type="telegram.review_sent",
                payload={
                    "source_kind": row.source_kind,
                    "source_id": str(row.source_id),
                    "channel_profile_id": (
                        str(row.channel_profile_id)
                        if row.channel_profile_id
                        else None
                    ),
                    "chat_id": row.chat_id,
                    "message_id": message_id,
                },
            )
        )


def mark_failed(session_id: uuid.UUID, error: str) -> None:
    with session_scope() as session:
        row = session.get(TelegramReviewSession, session_id)
        if row is None:
            return
        metadata = dict(row.session_metadata or {})
        attempts = int(metadata.get("delivery_attempts") or 0) + 1
        metadata["delivery_attempts"] = attempts
        row.session_metadata = metadata
        row.state = "failed" if attempts >= 3 else "queued"
        row.last_error = error[:2000]
        session.add(
            DomainEvent(
                aggregate_type="telegram_review",
                aggregate_id=str(row.id),
                event_type="telegram.review_delivery_failed",
                payload={
                    "source_kind": row.source_kind,
                    "source_id": str(row.source_id),
                    "attempt": attempts,
                    "will_retry": attempts < 3,
                    "error": error[:500],
                },
            )
        )


def session_for_callback(callback_token: str) -> TelegramReviewSession:
    with session_scope() as session:
        row = session.scalar(
            select(TelegramReviewSession).where(
                TelegramReviewSession.callback_token == callback_token
            )
        )
        if row is None:
            raise ValueError("Telegram review callback is stale or unknown")
        session.expunge(row)
        return row


def set_session_state(
    session_id: uuid.UUID,
    state: str,
    *,
    prompt_message_id: int | None = None,
    metadata: dict[str, object] | None = None,
) -> None:
    with session_scope() as session:
        row = session.get(TelegramReviewSession, session_id)
        if row is None:
            raise ValueError("Telegram review session disappeared")
        row.state = state
        if prompt_message_id is not None:
            row.feedback_prompt_message_id = prompt_message_id
        if metadata:
            row.session_metadata = {**dict(row.session_metadata or {}), **metadata}


def feedback_session(
    *,
    chat_id: int,
    reply_to_message_id: int,
) -> TelegramReviewSession | None:
    with session_scope() as session:
        row = session.scalar(
            select(TelegramReviewSession).where(
                TelegramReviewSession.chat_id == chat_id,
                TelegramReviewSession.feedback_prompt_message_id == reply_to_message_id,
                TelegramReviewSession.state == "awaiting_edit_feedback",
            )
        )
        if row is None:
            return None
        session.expunge(row)
        return row


def backlogged_sessions(limit: int = 10) -> list[TelegramReviewSession]:
    with session_scope() as session:
        rows = list(
            session.scalars(
                select(TelegramReviewSession)
                .where(TelegramReviewSession.state == "backlogged")
                .order_by(TelegramReviewSession.updated_at)
                .limit(limit)
            )
        )
        for row in rows:
            session.expunge(row)
        return rows


def render_bytes(card: TelegramReviewCard, *, settings: Settings | None = None) -> bytes:
    settings = settings or get_settings()
    store = ObjectStore(settings)
    metadata = store.stat(card.render_key)
    max_bytes = settings.telegram_video_max_mb * 1024 * 1024
    size = int(metadata.get("size_bytes") or 0)
    if size <= 0:
        raise ValueError("Telegram review render is empty")
    if size > max_bytes:
        raise ValueError(
            f"Telegram review render is {size / 1024 / 1024:.1f} MB; "
            f"configured Bot API limit is {settings.telegram_video_max_mb} MB"
        )
    return store.get_bytes(card.render_key)
