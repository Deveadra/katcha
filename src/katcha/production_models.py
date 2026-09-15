from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import JSON, DateTime, ForeignKey, Numeric, String, Text, Uuid, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from katcha.db import Base
from katcha.domain import ProductionStatus


class Production(Base):
    __tablename__ = "productions"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    clip_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("clips.id"), index=True
    )
    parent_production_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("productions.id"), nullable=True, index=True
    )
    generation: Mapped[int] = mapped_column(default=1)
    regenerate_from: Mapped[str | None] = mapped_column(String(32), nullable=True)
    workflow_id: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    kind: Mapped[str] = mapped_column(String(32), default="short", index=True)
    status: Mapped[str] = mapped_column(
        String(32), default=ProductionStatus.QUEUED.value, index=True
    )
    stage: Mapped[str] = mapped_column(String(64), default="queued")
    persona_key: Mapped[str] = mapped_column(String(64))
    persona_version: Mapped[str] = mapped_column(String(32))
    prompt_version: Mapped[str] = mapped_column(String(32))
    analysis_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    selected_script_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
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


class ProductionScript(Base):
    __tablename__ = "production_scripts"
    __table_args__ = (UniqueConstraint("production_id", "candidate_index"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    production_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("productions.id"), index=True
    )
    candidate_index: Mapped[int] = mapped_column()
    style: Mapped[str] = mapped_column(String(64))
    narration: Mapped[str] = mapped_column(Text)
    interaction_prompt: Mapped[str | None] = mapped_column(Text, nullable=True)
    rationale: Mapped[str | None] = mapped_column(Text, nullable=True)
    provider: Mapped[str] = mapped_column(String(32))
    model: Mapped[str] = mapped_column(String(128))
    prompt_version: Mapped[str] = mapped_column(String(32))
    selected: Mapped[bool] = mapped_column(default=False, index=True)
    script_metadata: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ProductionAsset(Base):
    __tablename__ = "production_assets"
    __table_args__ = (UniqueConstraint("production_id", "kind", "generation"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    production_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("productions.id"), index=True
    )
    kind: Mapped[str] = mapped_column(String(64), index=True)
    generation: Mapped[int] = mapped_column(default=1)
    storage_key: Mapped[str] = mapped_column(Text)
    content_type: Mapped[str | None] = mapped_column(String(128), nullable=True)
    provider: Mapped[str | None] = mapped_column(String(32), nullable=True)
    model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    asset_metadata: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ProductionReview(Base):
    __tablename__ = "production_reviews"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    production_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("productions.id"), index=True
    )
    decision: Mapped[str] = mapped_column(String(32), index=True)
    actor: Mapped[str] = mapped_column(String(128), default="operator")
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    review_metadata: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
