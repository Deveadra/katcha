from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
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
    __table_args__ = (
        CheckConstraint("revision >= 0", name="ck_editorial_project_revision"),
        UniqueConstraint("id", "channel_profile_id", name="uq_editorial_project_channel_scope"),
    )

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


class ProjectMediaItem(Base):
    """A project-scoped reference to canonical media or an external source link."""

    __tablename__ = "project_media_items"
    __table_args__ = (
        CheckConstraint("revision > 0", name="ck_project_media_item_revision"),
        CheckConstraint(
            "(clip_id IS NOT NULL AND external_reference_url IS NULL) OR "
            "(clip_id IS NULL AND external_reference_url IS NOT NULL)",
            name="ck_project_media_item_single_source",
        ),
        CheckConstraint(
            "status IN ('reference_only', 'review_required', 'denied', 'acquired', 'ready')",
            name="ck_project_media_item_status",
        ),
        CheckConstraint(
            "rights_status IN ('not_assessed', 'reference_only', 'review_permitted', "
            "'production_cleared', 'restricted', 'denied', 'revoked', 'expired')",
            name="ck_project_media_item_rights_status",
        ),
        CheckConstraint(
            "status != 'ready' OR (rights_status = 'production_cleared' AND clip_id IS NOT NULL)",
            name="ck_project_media_item_ready_requires_cleared_clip",
        ),
        CheckConstraint(
            "(rights_assessment_id IS NULL AND rights_assessment_version IS NULL) OR "
            "(rights_assessment_id IS NOT NULL AND rights_assessment_version > 0)",
            name="ck_project_media_item_rights_assessment_version",
        ),
        CheckConstraint(
            "rights_status != 'production_cleared' OR rights_assessment_id IS NOT NULL",
            name="ck_project_media_item_clearance_has_assessment",
        ),
        ForeignKeyConstraint(
            ["project_id", "channel_profile_id"],
            ["editorial_projects.id", "editorial_projects.channel_profile_id"],
            name="fk_project_media_item_project_channel",
        ),
        ForeignKeyConstraint(
            ["project_id", "revision"],
            ["editorial_revisions.project_id", "editorial_revisions.revision"],
            name="fk_project_media_item_revision",
        ),
        UniqueConstraint("request_id", name="uq_project_media_item_request"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    project_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, index=True)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    channel_profile_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("channel_profiles.id"), nullable=False, index=True
    )
    clip_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("clips.id"), nullable=True, index=True
    )
    external_reference_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    request_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    request_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    rights_status: Mapped[str] = mapped_column(String(32), nullable=False, default="not_assessed")
    rights_assessment_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("rights_assessments.id"), nullable=True
    )
    rights_assessment_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    requirement_ids: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    searchable_tags: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    provenance: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    source_metadata: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    source_in_seconds: Mapped[float | None] = mapped_column(nullable=True)
    source_out_seconds: Mapped[float | None] = mapped_column(nullable=True)
    notes: Mapped[str] = mapped_column(Text, nullable=False, default="")
    actor: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_verified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class AcquisitionReceipt(Base):
    """Durable, idempotent record of an authorized provider acquisition attempt."""

    __tablename__ = "acquisition_receipts"
    __table_args__ = (
        UniqueConstraint(
            "channel_profile_id", "operation_key", name="uq_acquisition_receipt_operation"
        ),
        CheckConstraint(
            "status IN ('authorized', 'acquiring', 'succeeded', 'failed', 'unknown')",
            name="ck_acquisition_receipt_status",
        ),
        CheckConstraint(
            "bytes_received IS NULL OR bytes_received >= 0",
            name="ck_acquisition_receipt_bytes",
        ),
        CheckConstraint(
            "status != 'succeeded' OR (sha256 IS NOT NULL AND object_store_key IS NOT NULL)",
            name="ck_acquisition_receipt_success_evidence",
        ),
        CheckConstraint(
            "status != 'unknown' OR error_category IS NOT NULL",
            name="ck_acquisition_receipt_unknown_reason",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    channel_profile_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("channel_profiles.id"), nullable=False, index=True
    )
    candidate_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("discovery_candidates.id"), nullable=False, index=True
    )
    operation_key: Mapped[str] = mapped_column(String(160), nullable=False)
    request_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    authorization_scope: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    fetch_url_if_allowed: Mapped[str | None] = mapped_column(Text, nullable=True)
    request_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    response_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    bytes_received: Mapped[int | None] = mapped_column(Integer, nullable=True)
    sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    mime: Mapped[str | None] = mapped_column(String(160), nullable=True)
    media_probe: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    object_store_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    duplicate_of: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    source_version: Mapped[str | None] = mapped_column(String(255), nullable=True)
    download_terms: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    cost_receipt: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    error_category: Mapped[str | None] = mapped_column(String(80), nullable=True)
    safe_resume_cursor: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
