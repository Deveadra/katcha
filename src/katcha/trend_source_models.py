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


class TrendSourceSubscription(Base):
    __tablename__ = "trend_source_subscriptions"
    __table_args__ = (
        UniqueConstraint("channel_profile_id", "subscription_key"),
        CheckConstraint(
            "poll_interval_seconds >= 60",
            name="ck_trend_source_poll_interval_minimum",
        ),
        CheckConstraint(
            "consecutive_failures >= 0",
            name="ck_trend_source_failures_nonnegative",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    channel_profile_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("channel_profiles.id"), index=True
    )
    subscription_key: Mapped[str] = mapped_column(String(128), index=True)
    name: Mapped[str] = mapped_column(String(255))
    adapter_key: Mapped[str] = mapped_column(String(64), index=True)
    adapter_version: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(32), default="active", index=True)
    source_query: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    cursor: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    poll_interval_seconds: Mapped[int] = mapped_column(Integer, default=900)
    health_status: Mapped[str] = mapped_column(String(32), default="unknown", index=True)
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0)
    last_success_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_failure_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    next_poll_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    backoff_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    last_error_kind: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_error_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_metadata: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class TrendSourcePoll(Base):
    __tablename__ = "trend_source_polls"
    __table_args__ = (
        UniqueConstraint("trend_source_subscription_id", "run_key"),
        CheckConstraint("item_count >= 0", name="ck_trend_source_poll_item_count"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    trend_source_subscription_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("trend_source_subscriptions.id"),
        index=True,
    )
    run_key: Mapped[str] = mapped_column(String(160), index=True)
    status: Mapped[str] = mapped_column(String(32), index=True)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    item_count: Mapped[int] = mapped_column(Integer, default=0)
    cursor_before: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    cursor_after: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    error_kind: Mapped[str | None] = mapped_column(String(64), nullable=True)
    retry_after_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    poll_metadata: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
