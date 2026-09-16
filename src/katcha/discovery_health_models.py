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


class DiscoverySourceState(Base):
    __tablename__ = "discovery_source_states"
    __table_args__ = (
        UniqueConstraint("source_key"),
        CheckConstraint("consecutive_failures >= 0", name="ck_source_failures_nonnegative"),
        CheckConstraint("total_polls >= 0", name="ck_source_total_polls_nonnegative"),
        CheckConstraint("quota_used >= 0", name="ck_source_quota_used_nonnegative"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    source_key: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    adapter_key: Mapped[str] = mapped_column(String(64), index=True)
    adapter_version: Mapped[str] = mapped_column(String(64))
    query_fingerprint: Mapped[str] = mapped_column(String(64), index=True)
    effective_query: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    health_status: Mapped[str] = mapped_column(String(32), default="unknown", index=True)
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0)
    total_polls: Mapped[int] = mapped_column(Integer, default=0)
    total_successes: Mapped[int] = mapped_column(Integer, default=0)
    total_empty_successes: Mapped[int] = mapped_column(Integer, default=0)
    total_failures: Mapped[int] = mapped_column(Integer, default=0)
    total_rate_limits: Mapped[int] = mapped_column(Integer, default=0)
    cursor: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    last_outcome: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    last_error_kind: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_poll_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    last_success_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_failure_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    next_eligible_poll_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    rate_limit_reset_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    quota_limit_per_day: Mapped[int | None] = mapped_column(Integer, nullable=True)
    quota_used: Mapped[int] = mapped_column(Integer, default=0)
    quota_window_started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    state_metadata: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class DiscoveryPollAttempt(Base):
    __tablename__ = "discovery_poll_attempts"
    __table_args__ = (
        UniqueConstraint("source_state_id", "attempt_key"),
        CheckConstraint("candidate_count >= 0", name="ck_poll_candidate_count_nonnegative"),
        CheckConstraint("pages >= 0", name="ck_poll_pages_nonnegative"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    source_state_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("discovery_source_states.id"), index=True
    )
    discovery_run_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("discovery_runs.id"), nullable=True, index=True
    )
    attempt_key: Mapped[str] = mapped_column(String(160), index=True)
    outcome: Mapped[str] = mapped_column(String(32), default="running", index=True)
    candidate_count: Mapped[int] = mapped_column(Integer, default=0)
    pages: Mapped[int] = mapped_column(Integer, default=0)
    cursor_before: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    cursor_after: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    http_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    retry_after_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error_kind: Mapped[str | None] = mapped_column(String(64), nullable=True)
    attempt_metadata: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
