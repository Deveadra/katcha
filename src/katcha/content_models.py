"""Persistent channel-scoped manual content requests and resumable file intake."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
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


class ContentItem(Base):
    __tablename__ = "content_items"
    __table_args__ = (
        UniqueConstraint("channel_profile_id", "request_key", name="uq_content_channel_request"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    channel_profile_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("channel_profiles.id"), index=True
    )
    request_key: Mapped[str] = mapped_column(String(160))
    input_kind: Mapped[str] = mapped_column(String(32))
    title: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(32), default="received", index=True)
    source_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("source_items.id"), nullable=True, index=True
    )
    clip_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("clips.id"), nullable=True, index=True
    )
    production_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("productions.id"), nullable=True
    )
    filename: Mapped[str | None] = mapped_column(Text, nullable=True)
    size_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    upload_offset: Mapped[int] = mapped_column(BigInteger, default=0)
    details: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
