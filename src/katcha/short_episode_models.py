from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
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


class ShortEpisode(Base):
    __tablename__ = "short_episodes"
    __table_args__ = (
        CheckConstraint("item_count > 0", name="ck_short_episode_item_count_positive"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    channel_profile_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("channel_profiles.id"), index=True
    )
    trend_opportunity_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("trend_opportunities.id"), nullable=True, index=True
    )
    parent_episode_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("short_episodes.id"), nullable=True, index=True
    )
    generation: Mapped[int] = mapped_column(Integer, default=1)
    regenerate_from: Mapped[str | None] = mapped_column(String(32), nullable=True)
    workflow_id: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    status: Mapped[str] = mapped_column(String(32), default="planned", index=True)
    stage: Mapped[str] = mapped_column(String(64), default="planned")
    premise: Mapped[str] = mapped_column(Text)
    format_key: Mapped[str] = mapped_column(String(64), index=True)
    format_version: Mapped[str] = mapped_column(String(32))
    item_count: Mapped[int] = mapped_column(Integer)
    format_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    plan_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    persona_key: Mapped[str] = mapped_column(String(64))
    persona_version: Mapped[str] = mapped_column(String(32))
    prompt_version: Mapped[str] = mapped_column(String(32), default="ranked-episode-v1")
    brand_key: Mapped[str] = mapped_column(String(64), index=True)
    brand_version: Mapped[int] = mapped_column(Integer)
    brand_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    selected_script_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey(
            "short_episode_scripts.id",
            name="fk_short_episodes_selected_script_id",
            use_alter=True,
        ),
        nullable=True,
        index=True,
    )
    selected_voice_profile: Mapped[str | None] = mapped_column(String(64), nullable=True)
    render_manifest: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    estimated_cost_usd: Mapped[Decimal] = mapped_column(
        Numeric(14, 8), default=Decimal("0")
    )
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class ShortEpisodeItem(Base):
    __tablename__ = "short_episode_items"
    __table_args__ = (
        UniqueConstraint("short_episode_id", "position"),
        UniqueConstraint("short_episode_id", "clip_id"),
        CheckConstraint("position > 0", name="ck_short_episode_item_position_positive"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    short_episode_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("short_episodes.id"), index=True
    )
    position: Mapped[int] = mapped_column(Integer)
    clip_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("clips.id"), index=True
    )
    role: Mapped[str] = mapped_column(String(32), index=True)
    overall_score: Mapped[Decimal] = mapped_column(Numeric(8, 4))
    role_score: Mapped[Decimal] = mapped_column(Numeric(8, 4))
    editorial_signals: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    analysis_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    acquisition_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    source_snapshot: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class ShortEpisodeScript(Base):
    __tablename__ = "short_episode_scripts"
    __table_args__ = (UniqueConstraint("short_episode_id", "candidate_index"),)

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    short_episode_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("short_episodes.id"), index=True
    )
    candidate_index: Mapped[int] = mapped_column(Integer)
    style: Mapped[str] = mapped_column(String(32), index=True)
    script_payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    narration_beats: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    provider: Mapped[str] = mapped_column(String(32))
    model: Mapped[str] = mapped_column(String(128))
    prompt_version: Mapped[str] = mapped_column(String(32))
    selected: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class ShortEpisodeAsset(Base):
    __tablename__ = "short_episode_assets"
    __table_args__ = (
        UniqueConstraint("short_episode_id", "kind", "generation"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    short_episode_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("short_episodes.id"), index=True
    )
    kind: Mapped[str] = mapped_column(String(64), index=True)
    generation: Mapped[int] = mapped_column(Integer, default=1)
    storage_key: Mapped[str] = mapped_column(Text)
    content_type: Mapped[str] = mapped_column(String(128))
    provider: Mapped[str | None] = mapped_column(String(32), nullable=True)
    model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    asset_metadata: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class ShortEpisodeReview(Base):
    __tablename__ = "short_episode_reviews"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    short_episode_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("short_episodes.id"), index=True
    )
    decision: Mapped[str] = mapped_column(String(32), index=True)
    actor: Mapped[str] = mapped_column(String(128), default="operator")
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    review_metadata: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
