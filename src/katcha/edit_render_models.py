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
        UniqueConstraint("workflow_id"),
        UniqueConstraint("production_id", "attempt_number"),
        UniqueConstraint("short_episode_id", "attempt_number"),
        CheckConstraint(
            """
            (production_id IS NOT NULL AND short_episode_id IS NULL)
            OR
            (production_id IS NULL AND short_episode_id IS NOT NULL)
            """,
            name="ck_render_attempt_exactly_one_source",
        ),
        CheckConstraint(
            "attempt_number > 0",
            name="ck_render_attempt_number_positive",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
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
    retry_of_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("render_attempts.id"),
        nullable=True,
        index=True,
    )
    attempt_number: Mapped[int] = mapped_column(Integer)
    workflow_id: Mapped[str] = mapped_column(String(255), index=True)
    status: Mapped[str] = mapped_column(String(32), default="queued", index=True)
    requested_by: Mapped[str] = mapped_column(String(128), default="workflow")
    manifest_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    manifest_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    manifest_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    output_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    pre_qc: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    post_qc: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    failure_kind: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    attempt_metadata: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
