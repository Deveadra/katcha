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


class EditorialStoryboardRevision(Base):
    __tablename__ = "editorial_storyboard_revisions"
    __table_args__ = (
        CheckConstraint(
            "script_revision > 0",
            name="ck_editorial_storyboard_script_revision",
        ),
        CheckConstraint("version > 0", name="ck_editorial_storyboard_version"),
        CheckConstraint(
            "parent_version IS NULL OR "
            "(parent_version > 0 AND parent_version < version)",
            name="ck_editorial_storyboard_parent_version",
        ),
        CheckConstraint(
            "origin IN ('operator', 'ai_apply', 'undo')",
            name="ck_editorial_storyboard_origin",
        ),
    )

    project_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("editorial_projects.id"), primary_key=True
    )
    script_revision: Mapped[int] = mapped_column(Integer, primary_key=True)
    version: Mapped[int] = mapped_column(Integer, primary_key=True)
    channel_profile_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("channel_profiles.id"), index=True
    )
    parent_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    request_id: Mapped[uuid.UUID] = mapped_column(Uuid, unique=True)
    request_digest: Mapped[str] = mapped_column(String(64))
    digest: Mapped[str] = mapped_column(String(64))
    workspace: Mapped[dict[str, Any]] = mapped_column(JSON)
    origin: Mapped[str] = mapped_column(String(32))
    actor: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


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


class EditorialRenderReview(Base):
    __tablename__ = "editorial_render_reviews"
    __table_args__ = (
        UniqueConstraint("run_id", "sequence", name="uq_editorial_review_sequence"),
        CheckConstraint("sequence > 0", name="ck_editorial_review_sequence"),
        CheckConstraint(
            "decision IN ('approve', 'request_changes')", name="ck_editorial_review_decision"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    run_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("editorial_runs.id"), index=True)
    sequence: Mapped[int] = mapped_column(Integer)
    request_digest: Mapped[str] = mapped_column(String(64))
    manifest_digest: Mapped[str] = mapped_column(String(64))
    result_digest: Mapped[str] = mapped_column(String(64))
    decision: Mapped[str] = mapped_column(String(32))
    note: Mapped[str] = mapped_column(Text)
    actor: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class EditorialNarration(Base):
    __tablename__ = "editorial_narration"
    __table_args__ = (
        CheckConstraint("revision > 0", name="ck_editorial_narration_revision"),
        CheckConstraint("sample_frames > 0", name="ck_editorial_narration_frames"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    project_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("editorial_projects.id"), index=True
    )
    channel_profile_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("channel_profiles.id"), index=True
    )
    revision: Mapped[int] = mapped_column(Integer)
    beat_id: Mapped[str] = mapped_column(String(120))
    text_digest: Mapped[str] = mapped_column(String(64))
    sha256: Mapped[str] = mapped_column(String(64))
    storage_key: Mapped[str] = mapped_column(String(1000))
    sample_rate: Mapped[int] = mapped_column(Integer)
    sample_frames: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(32), default="active")
    actor: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class EditorialImage(Base):
    __tablename__ = "editorial_images"
    __table_args__ = (
        CheckConstraint("revision > 0", name="ck_editorial_image_revision"),
        CheckConstraint("width > 0 AND height > 0", name="ck_editorial_image_dimensions"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    project_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("editorial_projects.id"), index=True
    )
    channel_profile_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("channel_profiles.id"), index=True
    )
    revision: Mapped[int] = mapped_column(Integer)
    beat_id: Mapped[str] = mapped_column(String(120))
    request_digest: Mapped[str] = mapped_column(String(64))
    sha256: Mapped[str] = mapped_column(String(64))
    storage_key: Mapped[str] = mapped_column(String(1000))
    width: Mapped[int] = mapped_column(Integer)
    height: Mapped[int] = mapped_column(Integer)
    title: Mapped[str] = mapped_column(String(200))
    source_reference: Mapped[str] = mapped_column(String(2000))
    use_note: Mapped[str] = mapped_column(String(2000))
    source_metadata: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    illustration: Mapped[bool] = mapped_column(default=False)
    status: Mapped[str] = mapped_column(String(32), default="active")
    actor: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
