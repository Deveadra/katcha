from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from katcha.db import Base
from katcha.domain import PublicationStatus, YouTubeConnectionStatus


class YouTubeConnection(Base):
    __tablename__ = "youtube_connections"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    channel_id: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    channel_title: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(
        String(32), default=YouTubeConnectionStatus.ACTIVE.value, index=True
    )
    scopes: Mapped[list[str]] = mapped_column(JSON, default=list)
    encrypted_access_token: Mapped[str] = mapped_column(Text)
    encrypted_refresh_token: Mapped[str] = mapped_column(Text)
    token_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    connection_metadata: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    last_refreshed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class OAuthState(Base):
    __tablename__ = "oauth_states"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    state_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    encrypted_code_verifier: Mapped[str] = mapped_column(Text)
    redirect_uri: Mapped[str] = mapped_column(Text)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Publication(Base):
    __tablename__ = "publications"
    __table_args__ = (UniqueConstraint("production_id", "youtube_connection_id"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    production_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("productions.id"), index=True
    )
    youtube_connection_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("youtube_connections.id"), index=True
    )
    workflow_id: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    workflow_attempt: Mapped[int] = mapped_column(default=1)
    analytics_workflow_id: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    status: Mapped[str] = mapped_column(
        String(32), default=PublicationStatus.QUEUED.value, index=True
    )
    stage: Mapped[str] = mapped_column(String(64), default="queued")
    title: Mapped[str] = mapped_column(String(100))
    description: Mapped[str] = mapped_column(Text, default="")
    tags: Mapped[list[str]] = mapped_column(JSON, default=list)
    category_id: Mapped[str] = mapped_column(String(32), default="24")
    privacy_status: Mapped[str] = mapped_column(String(32), default="private")
    publish_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    notify_subscribers: Mapped[bool] = mapped_column(Boolean, default=False)
    made_for_kids: Mapped[bool] = mapped_column(Boolean, default=False)
    contains_synthetic_media: Mapped[bool] = mapped_column(Boolean, default=False)
    youtube_video_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    encrypted_upload_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    upload_offset: Mapped[int] = mapped_column(BigInteger, default=0)
    upload_size: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    processing_status: Mapped[str | None] = mapped_column(String(64), nullable=True)
    failure_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    rejection_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    raw_status: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class PublicationAnalyticsSnapshot(Base):
    __tablename__ = "publication_analytics_snapshots"
    __table_args__ = (UniqueConstraint("publication_id", "sample_key"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    publication_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("publications.id"), index=True
    )
    sample_key: Mapped[str] = mapped_column(String(128))
    sampled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    period_start: Mapped[date] = mapped_column(Date)
    period_end: Mapped[date] = mapped_column(Date)
    views: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    engaged_views: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    estimated_minutes_watched: Mapped[Decimal | None] = mapped_column(Numeric(18, 4), nullable=True)
    average_view_duration: Mapped[Decimal | None] = mapped_column(Numeric(18, 4), nullable=True)
    average_view_percentage: Mapped[Decimal | None] = mapped_column(Numeric(12, 6), nullable=True)
    likes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    comments: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    shares: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    subscribers_gained: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    subscribers_lost: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    estimated_revenue: Mapped[Decimal | None] = mapped_column(Numeric(18, 8), nullable=True)
    estimated_ad_revenue: Mapped[Decimal | None] = mapped_column(Numeric(18, 8), nullable=True)
    monetized_playbacks: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    raw_metrics: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    raw_monetary: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    raw_video: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class RetentionPoint(Base):
    __tablename__ = "retention_points"
    __table_args__ = (UniqueConstraint("snapshot_id", "elapsed_video_time_ratio"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    snapshot_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("publication_analytics_snapshots.id"), index=True
    )
    elapsed_video_time_ratio: Mapped[Decimal] = mapped_column(Numeric(8, 6))
    audience_watch_ratio: Mapped[Decimal | None] = mapped_column(Numeric(12, 8), nullable=True)
    relative_retention_performance: Mapped[Decimal | None] = mapped_column(
        Numeric(12, 8), nullable=True
    )
    raw_row: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
