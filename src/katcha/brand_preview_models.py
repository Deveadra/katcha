from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import JSON, DateTime, ForeignKey, String, Text, UniqueConstraint, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column

from katcha.db import Base


class BrandPreviewRender(Base):
    __tablename__ = "brand_preview_renders"
    __table_args__ = (
        UniqueConstraint(
            "channel_profile_id",
            "request_key",
            name="uq_brand_preview_channel_request",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    channel_profile_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("channel_profiles.id"), index=True
    )
    production_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("productions.id"), index=True
    )
    brand_version_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("channel_brand_versions.id"), index=True
    )
    brand_key: Mapped[str] = mapped_column(String(64), index=True)
    brand_version: Mapped[int] = mapped_column(index=True)
    request_key: Mapped[str] = mapped_column(String(64))
    workflow_id: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    status: Mapped[str] = mapped_column(String(32), default="queued", index=True)
    source_lineage: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    brand_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    reaction_cue: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    render_manifest: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    output_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    verification: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
