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
    Index,
    Integer,
    String,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from katcha.db import Base


class ChannelEditBlueprintVersion(Base):
    __tablename__ = "channel_edit_blueprint_versions"
    __table_args__ = (
        UniqueConstraint("channel_profile_id", "blueprint_key", "version"),
        CheckConstraint(
            "version > 0",
            name="ck_channel_edit_blueprint_version_positive",
        ),
        Index(
            "uq_channel_edit_blueprint_active_family",
            "channel_profile_id",
            "blueprint_key",
            unique=True,
            postgresql_where=text("is_active"),
            sqlite_where=text("is_active = 1"),
        ),
        Index(
            "uq_channel_edit_blueprint_default",
            "channel_profile_id",
            unique=True,
            postgresql_where=text("is_default"),
            sqlite_where=text("is_default = 1"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    channel_profile_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("channel_profiles.id"), index=True
    )
    blueprint_key: Mapped[str] = mapped_column(String(96), index=True)
    version: Mapped[int] = mapped_column(Integer)
    contract_version: Mapped[str] = mapped_column(String(32))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    contract: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    blueprint_metadata: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
