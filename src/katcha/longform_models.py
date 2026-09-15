from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    JSON,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from katcha.db import Base
from katcha.domain import CompilationStatus


class Compilation(Base):
    __tablename__ = "compilations"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    channel_profile_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("channel_profiles.id"), nullable=True, index=True
    )
    parent_compilation_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("compilations.id"), nullable=True, index=True
    )
    generation: Mapped[int] = mapped_column(Integer, default=1)
    regenerate_from: Mapped[str | None] = mapped_column(String(32), nullable=True)
    workflow_id: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    status: Mapped[str] = mapped_column(
        String(32), default=CompilationStatus.QUEUED.value, index=True
    )
    stage: Mapped[str] = mapped_column(String(64), default="queued")
    theme: Mapped[str] = mapped_column(Text)
    target_duration_seconds: Mapped[int] = mapped_column(Integer)
    target_segment_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    persona_key: Mapped[str] = mapped_column(String(64), default="youth_host")
    persona_version: Mapped[str] = mapped_column(String(32), default="1")
    prompt_version: Mapped[str] = mapped_column(String(32), default="longform-v1")
    candidate_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    editor_plan: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    critic_feedback: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    final_plan: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    selected_voice_profile: Mapped[str | None] = mapped_column(String(128), nullable=True)
    render_manifest: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    estimated_cost_usd: Mapped[Decimal] = mapped_column(
        Numeric(14, 8), default=Decimal("0")
    )
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class CompilationSegment(Base):
    __tablename__ = "compilation_segments"
    __table_args__ = (
        UniqueConstraint("compilation_id", "position"),
        UniqueConstraint("compilation_id", "clip_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    compilation_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("compilations.id"), index=True
    )
    position: Mapped[int] = mapped_column(Integer)
    clip_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("clips.id"), index=True
    )
    short_production_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("productions.id"), nullable=True, index=True
    )
    short_publication_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("publications.id"), nullable=True, index=True
    )
    deterministic_score: Mapped[Decimal] = mapped_column(Numeric(8, 6))
    opening_score: Mapped[Decimal] = mapped_column(Numeric(8, 6))
    source_duration_seconds: Mapped[Decimal] = mapped_column(Numeric(12, 3))
    source_start_seconds: Mapped[Decimal] = mapped_column(Numeric(12, 3), default=Decimal("0"))
    source_end_seconds: Mapped[Decimal | None] = mapped_column(Numeric(12, 3), nullable=True)
    transition_before: Mapped[str | None] = mapped_column(Text, nullable=True)
    host_before: Mapped[str | None] = mapped_column(Text, nullable=True)
    host_after: Mapped[str | None] = mapped_column(Text, nullable=True)
    selection_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    evidence: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    timing: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class CompilationAsset(Base):
    __tablename__ = "compilation_assets"
    __table_args__ = (UniqueConstraint("compilation_id", "kind", "generation"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    compilation_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("compilations.id"), index=True
    )
    kind: Mapped[str] = mapped_column(String(64), index=True)
    generation: Mapped[int] = mapped_column(Integer, default=1)
    storage_key: Mapped[str] = mapped_column(Text)
    content_type: Mapped[str | None] = mapped_column(String(128), nullable=True)
    provider: Mapped[str | None] = mapped_column(String(32), nullable=True)
    model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    asset_metadata: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class CompilationReview(Base):
    __tablename__ = "compilation_reviews"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    compilation_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("compilations.id"), index=True
    )
    decision: Mapped[str] = mapped_column(String(32), index=True)
    actor: Mapped[str] = mapped_column(String(128), default="operator")
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    review_metadata: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
