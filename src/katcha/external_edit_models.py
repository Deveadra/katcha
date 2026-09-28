from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    CheckConstraint,
    DateTime,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from katcha.db import Base


class ExternalEditHandoff(Base):
    __tablename__ = "external_edit_handoffs"
    __table_args__ = (
        UniqueConstraint(
            "provider",
            "source_type",
            "source_id",
            "generation",
            name="uq_external_edit_handoff_generation",
        ),
        CheckConstraint(
            "provider IN ('invideo')",
            name="ck_external_edit_provider",
        ),
        CheckConstraint(
            "source_type IN ('production', 'short_episode')",
            name="ck_external_edit_source_type",
        ),
        CheckConstraint(
            "status IN ('prepared', 'output_imported', 'adopted', 'cancelled', 'failed')",
            name="ck_external_edit_status",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    provider: Mapped[str] = mapped_column(String(32), index=True)
    source_type: Mapped[str] = mapped_column(String(32), index=True)
    source_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), index=True)
    generation: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(32), default="prepared", index=True)
    package_manifest_key: Mapped[str] = mapped_column(Text)
    output_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    external_project_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    handoff_metadata: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
