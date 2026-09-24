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


class TrendReviewQueueItem(Base):
    __tablename__ = "trend_review_queue_items"
    __table_args__ = (
        UniqueConstraint(
            "topic_watch_id",
            "queue_key",
            "discovery_candidate_id",
        ),
        CheckConstraint("rank > 0", name="ck_trend_review_queue_rank_positive"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    topic_watch_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("topic_watch_versions.id"), index=True
    )
    discovery_candidate_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("discovery_candidates.id"), index=True
    )
    trend_score_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("candidate_trend_scores.id"), index=True
    )
    queue_key: Mapped[str] = mapped_column(String(160), index=True)
    rank: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(32), default="pending", index=True)
    queue_metadata: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class TrendWatchSourceState(Base):
    __tablename__ = "trend_watch_source_states"
    __table_args__ = (
        UniqueConstraint("topic_watch_id", "adapter_index"),
        CheckConstraint(
            "adapter_index >= 0",
            name="ck_trend_watch_source_adapter_index_nonnegative",
        ),
        CheckConstraint(
            "consecutive_failures >= 0",
            name="ck_trend_watch_source_failures_nonnegative",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    topic_watch_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("topic_watch_versions.id"), index=True
    )
    adapter_index: Mapped[int] = mapped_column(Integer)
    adapter_key: Mapped[str] = mapped_column(String(64), index=True)
    adapter_version: Mapped[str] = mapped_column(String(64))
    source_identity: Mapped[str | None] = mapped_column(
        String(64), nullable=True, index=True
    )
    source_quota_limit_per_day: Mapped[int | None] = mapped_column(
        Integer, nullable=True
    )
    health_status: Mapped[str] = mapped_column(String(32), default="unknown", index=True)
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0)
    cursor: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    last_poll_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_discovery_run_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("discovery_runs.id"),
        nullable=True,
        index=True,
    )
    last_success_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_failure_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    backoff_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    rate_limit_reset_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    last_error_kind: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_error_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    state_metadata: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
