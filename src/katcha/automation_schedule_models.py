from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    Integer,
    String,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from katcha.db import Base


class AutomationSchedule(Base):
    __tablename__ = "automation_schedules"
    __table_args__ = (
        UniqueConstraint("schedule_key", name="uq_automation_schedule_key"),
        UniqueConstraint("workflow_id", name="uq_automation_schedule_workflow"),
        CheckConstraint(
            "generation > 0",
            name="ck_automation_schedule_generation_positive",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    schedule_key: Mapped[str] = mapped_column(String(255), index=True)
    schedule_kind: Mapped[str] = mapped_column(String(64), index=True)
    subject_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), index=True)
    workflow_id: Mapped[str] = mapped_column(String(255), index=True)
    supersedes_workflow_id: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
    )
    generation: Mapped[int] = mapped_column(Integer, default=1)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    schedule_config: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
    )
