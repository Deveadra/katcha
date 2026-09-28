from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
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


class ClipLifecycle(Base):
    __tablename__ = "clip_lifecycle"
    __table_args__ = (
        CheckConstraint(
            "lifecycle_state IN ('hot', 'archived', 'purged')",
            name="ck_clip_lifecycle_state",
        ),
    )

    clip_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("clips.id"), primary_key=True
    )
    lifecycle_state: Mapped[str] = mapped_column(
        String(32), default="hot", index=True
    )
    archive_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    tags: Mapped[list[str]] = mapped_column(JSON, default=list)
    library_metadata: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    search_document: Mapped[str] = mapped_column(Text, default="")
    embedding_metadata: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    archived_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    purged_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class ClipRetentionPolicy(Base):
    __tablename__ = "clip_retention_policies"
    __table_args__ = (
        UniqueConstraint("channel_profile_id", name="uq_clip_retention_channel"),
        CheckConstraint(
            "retention_mode IN ('indefinite', 'managed')",
            name="ck_clip_retention_mode",
        ),
        CheckConstraint(
            "archive_after_days IS NULL OR archive_after_days > 0",
            name="ck_clip_retention_archive_days",
        ),
        CheckConstraint(
            "purge_after_days IS NULL OR purge_after_days > 0",
            name="ck_clip_retention_purge_days",
        ),
        CheckConstraint(
            "failed_purge_after_days IS NULL OR failed_purge_after_days > 0",
            name="ck_clip_retention_failed_days",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    channel_profile_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("channel_profiles.id"),
        nullable=False,
        index=True,
    )
    retention_mode: Mapped[str] = mapped_column(
        String(32), default="indefinite", index=True
    )
    auto_archive: Mapped[bool] = mapped_column(Boolean, default=False)
    auto_purge: Mapped[bool] = mapped_column(Boolean, default=False)
    auto_remove_duplicates: Mapped[bool] = mapped_column(Boolean, default=False)
    archive_after_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    purge_after_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    failed_purge_after_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    confirmed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    confirmed_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    policy_metadata: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
