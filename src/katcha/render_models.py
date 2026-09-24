from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from katcha.db import Base


class RenderAttempt(Base):
    __tablename__ = "render_attempts"
    __table_args__ = (
        UniqueConstraint(
            "production_id",
            "attempt_number",
            name="uq_render_attempts_production_attempt",
        ),
        UniqueConstraint(
            "short_episode_id",
            "attempt_number",
            name="uq_render_attempts_short_episode_attempt",
        ),
        CheckConstraint(
            "(production_id IS NOT NULL AND short_episode_id IS NULL) OR "
            "(production_id IS NULL AND short_episode_id IS NOT NULL)",
            name="ck_render_attempts_exactly_one_source",
        ),
        CheckConstraint(
            "attempt_number > 0",
            name="ck_render_attempts_attempt_number_positive",
        ),
        CheckConstraint(
            "source_generation > 0",
            name="ck_render_attempts_source_generation_positive",
        ),
        CheckConstraint(
            "failure_count >= 0",
            name="ck_render_attempts_failure_count_nonnegative",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    attempt_key: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    production_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("productions.id"),
        nullable=True,
        index=True,
    )
    short_episode_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("short_episodes.id"),
        nullable=True,
        index=True,
    )
    channel_profile_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("channel_profiles.id"),
        nullable=True,
        index=True,
    )
    parent_attempt_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("render_attempts.id"),
        nullable=True,
        index=True,
    )
    source_generation: Mapped[int] = mapped_column(Integer)
    attempt_number: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(32), default="queued", index=True)
    stage: Mapped[str] = mapped_column(String(64), default="queued")
    output_key: Mapped[str] = mapped_column(Text)
    manifest_version: Mapped[str] = mapped_column(String(64))
    verification: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    failure_count: Mapped[int] = mapped_column(Integer, default=0)
    last_failure_class: Mapped[str | None] = mapped_column(String(128), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
