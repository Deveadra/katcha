"""Immutable proposal packages and append-only operator decisions."""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String, UniqueConstraint, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column

from katcha.db import Base


class RequirementShotPackage(Base):
    __tablename__ = "requirement_shot_packages"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    channel_profile_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("channel_profiles.id"))
    project_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("editorial_projects.id"))
    input_revision: Mapped[int] = mapped_column(Integer)
    request_digest: Mapped[str] = mapped_column(String(64))
    dependency_digest: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)
    actor: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class OperatorShotChoice(Base):
    __tablename__ = "operator_shot_choices"
    __table_args__ = (
        UniqueConstraint("project_id", "requirement_id", "version", name="uq_shot_choice_version"),
    )
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    channel_profile_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("channel_profiles.id"))
    project_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("editorial_projects.id"))
    requirement_id: Mapped[str] = mapped_column(String(200))
    version: Mapped[int] = mapped_column(Integer)
    request_digest: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)
    actor: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
