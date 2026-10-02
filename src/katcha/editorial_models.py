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
    Uuid,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from katcha.db import Base


class EditorialProject(Base):
    __tablename__ = "editorial_projects"
    __table_args__ = (CheckConstraint("revision >= 0", name="ck_editorial_project_revision"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    channel_profile_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("channel_profiles.id"), index=True
    )
    input_digest: Mapped[str] = mapped_column(String(64))
    brief: Mapped[dict[str, Any]] = mapped_column(JSON)
    revision: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class EditorialRevision(Base):
    __tablename__ = "editorial_revisions"
    __table_args__ = (CheckConstraint("revision > 0", name="ck_editorial_revision_positive"),)

    project_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("editorial_projects.id"), primary_key=True
    )
    revision: Mapped[int] = mapped_column(Integer, primary_key=True)
    request_id: Mapped[uuid.UUID] = mapped_column(Uuid, unique=True)
    request_digest: Mapped[str] = mapped_column(String(64))
    digest: Mapped[str] = mapped_column(String(64))
    draft: Mapped[dict[str, Any]] = mapped_column(JSON)
    actor: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class EditorialRun(Base):
    __tablename__ = "editorial_runs"
    __table_args__ = (CheckConstraint("attempt > 0", name="ck_editorial_run_attempt"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    project_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("editorial_projects.id"), index=True
    )
    channel_profile_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("channel_profiles.id"), index=True
    )
    input_digest: Mapped[str] = mapped_column(String(64))
    input_revision: Mapped[int] = mapped_column(Integer)
    options: Mapped[dict[str, Any]] = mapped_column(JSON)
    attempt: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(32), default="queued", index=True)
    stage: Mapped[str] = mapped_column(String(64), default="intake")
    artifacts: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    actor: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
