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
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from katcha.db import Base


class DiscoveryCollectionClaim(Base):
    __tablename__ = "discovery_collection_claims"
    __table_args__ = (
        UniqueConstraint("source_identity", "collection_window_key"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    source_identity: Mapped[str] = mapped_column(String(64), index=True)
    collection_window_key: Mapped[str] = mapped_column(String(64), index=True)
    owner_source_state_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("trend_watch_source_states.id"), index=True
    )
    discovery_run_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("discovery_runs.id"), nullable=True, index=True
    )
    status: Mapped[str] = mapped_column(String(32), default="running", index=True)
    lease_expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), index=True
    )
    blocked_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    claim_metadata: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class TrendWatchPollAttempt(Base):
    __tablename__ = "trend_watch_poll_attempts"
    __table_args__ = (
        UniqueConstraint("source_state_id", "execution_key"),
        CheckConstraint(
            "candidate_count >= 0",
            name="ck_trend_poll_candidate_count_nonnegative",
        ),
        CheckConstraint("pages >= 0", name="ck_trend_poll_pages_nonnegative"),
        CheckConstraint(
            "source_quota_reserved >= 0 AND source_quota_consumed >= 0",
            name="ck_trend_poll_source_quota_nonnegative",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    source_state_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("trend_watch_source_states.id"), index=True
    )
    collection_claim_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("discovery_collection_claims.id"),
        nullable=True,
        index=True,
    )
    discovery_run_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("discovery_runs.id"), nullable=True, index=True
    )
    execution_key: Mapped[str] = mapped_column(String(160), index=True)
    source_identity: Mapped[str] = mapped_column(String(64), index=True)
    outcome: Mapped[str] = mapped_column(String(32), default="reserved", index=True)
    cursor_before: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    cursor_after: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    candidate_count: Mapped[int] = mapped_column(Integer, default=0)
    pages: Mapped[int] = mapped_column(Integer, default=0)
    source_quota_reserved: Mapped[int] = mapped_column(Integer, default=0)
    source_quota_consumed: Mapped[int] = mapped_column(Integer, default=0)
    provider_usage: Mapped[dict[str, int]] = mapped_column(JSON, default=dict)
    http_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    retry_after_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error_kind: Mapped[str | None] = mapped_column(String(64), nullable=True)
    attempt_metadata: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class DiscoveryProviderQuotaWindow(Base):
    __tablename__ = "discovery_provider_quota_windows"
    __table_args__ = (
        UniqueConstraint("provider_key", "bucket_key", "window_key"),
        CheckConstraint("limit_units > 0", name="ck_discovery_quota_limit_positive"),
        CheckConstraint(
            "used_units >= 0 AND reserved_units >= 0",
            name="ck_discovery_quota_usage_nonnegative",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    provider_key: Mapped[str] = mapped_column(String(64), index=True)
    bucket_key: Mapped[str] = mapped_column(String(128), index=True)
    window_key: Mapped[str] = mapped_column(String(96), index=True)
    window_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    window_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    limit_units: Mapped[int] = mapped_column(Integer)
    used_units: Mapped[int] = mapped_column(Integer, default=0)
    reserved_units: Mapped[int] = mapped_column(Integer, default=0)
    blocked_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    quota_metadata: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class DiscoveryQuotaReservation(Base):
    __tablename__ = "discovery_quota_reservations"
    __table_args__ = (
        UniqueConstraint("poll_attempt_id", "page_key", "bucket_key"),
        CheckConstraint(
            "reserved_units > 0 AND consumed_units >= 0",
            name="ck_discovery_quota_reservation_units",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    poll_attempt_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("trend_watch_poll_attempts.id"), index=True
    )
    quota_window_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("discovery_provider_quota_windows.id"),
        index=True,
    )
    page_key: Mapped[str] = mapped_column(String(64), index=True)
    provider_key: Mapped[str] = mapped_column(String(64), index=True)
    bucket_key: Mapped[str] = mapped_column(String(128), index=True)
    window_key: Mapped[str] = mapped_column(String(96), index=True)
    reserved_units: Mapped[int] = mapped_column(Integer)
    consumed_units: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(32), default="reserved", index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    settled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
