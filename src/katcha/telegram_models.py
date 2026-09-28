from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from katcha.db import Base


class TelegramReviewSession(Base):
    __tablename__ = "telegram_review_sessions"
    __table_args__ = (
        UniqueConstraint(
            "source_kind",
            "source_id",
            "chat_id",
            name="uq_telegram_review_source_chat",
        ),
        CheckConstraint(
            "source_kind IN ('short_episode', 'production', 'compilation')",
            name="ck_telegram_review_source_kind",
        ),
        CheckConstraint(
            "state IN ("
            "'queued', 'sent', 'awaiting_edit_feedback', 'backlogged', "
            "'approved', 'rejected', 'regenerating', 'superseded', 'failed'"
            ")",
            name="ck_telegram_review_state",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    source_kind: Mapped[str] = mapped_column(String(32), index=True)
    source_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), index=True)
    channel_profile_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("channel_profiles.id"),
        nullable=True,
        index=True,
    )
    callback_token: Mapped[str] = mapped_column(String(24), unique=True, index=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, index=True)
    message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    telegram_file_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    state: Mapped[str] = mapped_column(String(32), default="queued", index=True)
    feedback_prompt_message_id: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True
    )
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    session_metadata: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class TelegramBotCursor(Base):
    __tablename__ = "telegram_bot_cursors"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    last_update_id: Mapped[int] = mapped_column(BigInteger, default=0)
    cursor_metadata: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
