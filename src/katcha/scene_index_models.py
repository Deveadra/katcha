"""Durable channel-scoped measured indexes and bounded indexing attempt receipts."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    CheckConstraint,
    DateTime,
    ForeignKey,
    String,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from katcha.db import Base


class MediaSceneIndex(Base):
    __tablename__ = "media_scene_indexes"
    __table_args__ = (
        UniqueConstraint(
            "channel_profile_id",
            "clip_id",
            "clip_sha256",
            "policy_version",
            name="uq_scene_index_content_policy",
        ),
        CheckConstraint(
            "status IN ('indexing', 'completed', 'failed')", name="ck_scene_index_status"
        ),
    )
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    channel_profile_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("channel_profiles.id"), nullable=False, index=True
    )
    clip_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("clips.id"), nullable=False)
    clip_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    policy_version: Mapped[str] = mapped_column(String(80), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    evidence: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    receipt: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    error_category: Mapped[str | None] = mapped_column(String(80), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
