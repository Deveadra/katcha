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
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from katcha.db import Base
from katcha.domain import AutomationLevel, ChannelStatus


class ChannelProfile(Base):
    __tablename__ = "channel_profiles"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    youtube_connection_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("youtube_connections.id"),
        unique=True,
        index=True,
    )
    status: Mapped[str] = mapped_column(
        String(32), default=ChannelStatus.ACTIVE.value, index=True
    )
    timezone: Mapped[str] = mapped_column(String(128), default="UTC")
    active_strategy_version: Mapped[int] = mapped_column(Integer, default=1)
    active_automation_version: Mapped[int] = mapped_column(Integer, default=1)
    profile_metadata: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class ChannelStrategyVersion(Base):
    __tablename__ = "channel_strategy_versions"
    __table_args__ = (
        UniqueConstraint("channel_profile_id", "version"),
        CheckConstraint(
            "monthly_base_budget_usd >= 0",
            name="ck_strategy_base_budget_nonnegative",
        ),
        CheckConstraint(
            "monthly_hard_budget_usd >= 0",
            name="ck_strategy_budget_nonnegative",
        ),
        CheckConstraint(
            "monthly_base_budget_usd <= monthly_hard_budget_usd",
            name="ck_strategy_base_within_hard_budget",
        ),
        CheckConstraint(
            "reinvestment_rate >= 0 AND reinvestment_rate <= 1",
            name="ck_strategy_reinvestment_rate",
        ),
        CheckConstraint(
            "reinvestment_cap_usd >= 0",
            name="ck_strategy_reinvestment_cap",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    channel_profile_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("channel_profiles.id"), index=True
    )
    version: Mapped[int] = mapped_column(Integer)
    monthly_base_budget_usd: Mapped[Decimal] = mapped_column(Numeric(14, 4))
    monthly_hard_budget_usd: Mapped[Decimal] = mapped_column(Numeric(14, 4))
    reinvestment_rate: Mapped[Decimal] = mapped_column(
        Numeric(8, 6), default=Decimal("1")
    )
    reinvestment_cap_usd: Mapped[Decimal] = mapped_column(
        Numeric(14, 4), default=Decimal("100")
    )
    fallback_schedule: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON, default=list
    )
    blackout_windows: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON, default=list
    )
    routing_policy: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    strategy_metadata: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class AutomationPolicyVersion(Base):
    __tablename__ = "automation_policy_versions"
    __table_args__ = (
        UniqueConstraint("channel_profile_id", "version"),
        CheckConstraint(
            "min_reviewed_items >= 0",
            name="ck_automation_min_reviewed",
        ),
        CheckConstraint(
            "min_approval_rate >= 0 AND min_approval_rate <= 1",
            name="ck_automation_approval_rate",
        ),
        CheckConstraint(
            "max_regeneration_rate >= 0 AND max_regeneration_rate <= 1",
            name="ck_automation_regeneration_rate",
        ),
        CheckConstraint(
            "max_publication_failure_rate >= 0 "
            "AND max_publication_failure_rate <= 1",
            name="ck_automation_publication_failure_rate",
        ),
        CheckConstraint(
            "min_ranking_confidence >= 0 AND min_ranking_confidence <= 1",
            name="ck_automation_ranking_confidence",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    channel_profile_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("channel_profiles.id"), index=True
    )
    version: Mapped[int] = mapped_column(Integer)
    level: Mapped[str] = mapped_column(
        String(64), default=AutomationLevel.REVIEW_REQUIRED.value, index=True
    )
    min_reviewed_items: Mapped[int] = mapped_column(Integer, default=50)
    min_approval_rate: Mapped[Decimal] = mapped_column(
        Numeric(8, 6), default=Decimal("0.90")
    )
    max_regeneration_rate: Mapped[Decimal] = mapped_column(
        Numeric(8, 6), default=Decimal("0.10")
    )
    max_publication_failure_rate: Mapped[Decimal] = mapped_column(
        Numeric(8, 6), default=Decimal("0.03")
    )
    min_ranking_confidence: Mapped[Decimal] = mapped_column(
        Numeric(8, 6), default=Decimal("0.60")
    )
    auto_demote: Mapped[bool] = mapped_column(Boolean, default=True)
    policy_metadata: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class PerformanceObservation(Base):
    __tablename__ = "performance_observations"
    __table_args__ = (UniqueConstraint("analytics_snapshot_id"),)

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    channel_profile_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("channel_profiles.id"), index=True
    )
    publication_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("publications.id"), index=True
    )
    analytics_snapshot_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("publication_analytics_snapshots.id"),
        unique=True,
        index=True,
    )
    source_kind: Mapped[str] = mapped_column(String(32), index=True)
    source_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), index=True)
    observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), index=True
    )
    publication_age_hours: Mapped[Decimal] = mapped_column(Numeric(14, 4))
    features: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    labels: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    outcome_score: Mapped[Decimal] = mapped_column(Numeric(8, 6), index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class RankingSnapshot(Base):
    __tablename__ = "ranking_snapshots"
    __table_args__ = (
        UniqueConstraint("channel_profile_id", "version"),
        UniqueConstraint("channel_profile_id", "run_key"),
        CheckConstraint("sample_count >= 0", name="ck_ranking_sample_count"),
        CheckConstraint(
            "blend_ratio >= 0 AND blend_ratio <= 1",
            name="ck_ranking_blend_ratio",
        ),
        CheckConstraint(
            "confidence >= 0 AND confidence <= 1",
            name="ck_ranking_confidence",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    channel_profile_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("channel_profiles.id"), index=True
    )
    version: Mapped[int] = mapped_column(Integer)
    run_key: Mapped[str] = mapped_column(String(128), index=True)
    algorithm: Mapped[str] = mapped_column(String(64))
    training_cutoff: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), index=True
    )
    sample_count: Mapped[int] = mapped_column(Integer)
    feature_names: Mapped[list[str]] = mapped_column(JSON, default=list)
    feature_means: Mapped[dict[str, float]] = mapped_column(JSON, default=dict)
    feature_scales: Mapped[dict[str, float]] = mapped_column(JSON, default=dict)
    coefficients: Mapped[dict[str, float]] = mapped_column(JSON, default=dict)
    intercept: Mapped[Decimal] = mapped_column(
        Numeric(14, 8), default=Decimal("0")
    )
    blend_ratio: Mapped[Decimal] = mapped_column(
        Numeric(8, 6), default=Decimal("0")
    )
    confidence: Mapped[Decimal] = mapped_column(
        Numeric(8, 6), default=Decimal("0")
    )
    validation_metrics: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class ChannelEconomicsSnapshot(Base):
    __tablename__ = "channel_economics_snapshots"
    __table_args__ = (UniqueConstraint("channel_profile_id", "sample_key"),)

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    channel_profile_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("channel_profiles.id"), index=True
    )
    sample_key: Mapped[str] = mapped_column(String(128))
    sampled_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), index=True
    )
    revenue_usd: Mapped[Decimal] = mapped_column(
        Numeric(18, 8), default=Decimal("0")
    )
    attributed_ai_cost_usd: Mapped[Decimal] = mapped_column(
        Numeric(18, 8), default=Decimal("0")
    )
    contribution_margin_usd: Mapped[Decimal] = mapped_column(
        Numeric(18, 8), default=Decimal("0")
    )
    reinvestable_usd: Mapped[Decimal] = mapped_column(
        Numeric(18, 8), default=Decimal("0")
    )
    base_budget_usd: Mapped[Decimal] = mapped_column(
        Numeric(18, 8), default=Decimal("0")
    )
    hard_budget_usd: Mapped[Decimal] = mapped_column(
        Numeric(18, 8), default=Decimal("0")
    )
    effective_budget_usd: Mapped[Decimal] = mapped_column(
        Numeric(18, 8), default=Decimal("0")
    )
    month_to_date_spend_usd: Mapped[Decimal] = mapped_column(
        Numeric(18, 8), default=Decimal("0")
    )
    reserved_ai_cost_usd: Mapped[Decimal] = mapped_column(
        Numeric(18, 8), default=Decimal("0")
    )
    budget_headroom_usd: Mapped[Decimal] = mapped_column(
        Numeric(18, 8), default=Decimal("0")
    )
    burn_rate_usd_per_day: Mapped[Decimal] = mapped_column(
        Numeric(18, 8), default=Decimal("0")
    )
    projected_month_end_spend_usd: Mapped[Decimal] = mapped_column(
        Numeric(18, 8), default=Decimal("0")
    )
    monetary_scope_available: Mapped[bool] = mapped_column(Boolean, default=False)
    details: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class AIBudgetReservation(Base):
    __tablename__ = "ai_budget_reservations"
    __table_args__ = (
        UniqueConstraint("channel_profile_id", "reservation_key"),
        CheckConstraint(
            "estimated_cost_usd >= 0",
            name="ck_budget_reservation_estimated_nonnegative",
        ),
        CheckConstraint(
            "actual_cost_usd IS NULL OR actual_cost_usd >= 0",
            name="ck_budget_reservation_actual_nonnegative",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    channel_profile_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("channel_profiles.id"), index=True
    )
    reservation_key: Mapped[str] = mapped_column(String(255))
    task: Mapped[str] = mapped_column(String(64), index=True)
    reference_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    reference_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    estimated_cost_usd: Mapped[Decimal] = mapped_column(Numeric(18, 8))
    actual_cost_usd: Mapped[Decimal | None] = mapped_column(
        Numeric(18, 8), nullable=True
    )
    status: Mapped[str] = mapped_column(String(32), default="reserved", index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    settled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    reservation_metadata: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class ScheduleRecommendation(Base):
    __tablename__ = "schedule_recommendations"
    __table_args__ = (
        UniqueConstraint("channel_profile_id", "run_key", "rank"),
        CheckConstraint(
            "weekday >= 0 AND weekday <= 6", name="ck_schedule_weekday"
        ),
        CheckConstraint(
            "hour_local >= 0 AND hour_local <= 23", name="ck_schedule_hour"
        ),
        CheckConstraint("sample_count >= 0", name="ck_schedule_sample_count"),
        CheckConstraint(
            "confidence >= 0 AND confidence <= 1",
            name="ck_schedule_confidence",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    channel_profile_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("channel_profiles.id"), index=True
    )
    run_key: Mapped[str] = mapped_column(String(128), index=True)
    rank: Mapped[int] = mapped_column(Integer)
    weekday: Mapped[int] = mapped_column(Integer)
    hour_local: Mapped[int] = mapped_column(Integer)
    score: Mapped[Decimal] = mapped_column(Numeric(8, 6))
    sample_count: Mapped[int] = mapped_column(Integer)
    confidence: Mapped[Decimal] = mapped_column(Numeric(8, 6))
    source: Mapped[str] = mapped_column(String(64))
    recommendation_metadata: Mapped[dict[str, Any]] = mapped_column(
        JSON, default=dict
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class EventConsumerCursor(Base):
    __tablename__ = "event_consumer_cursors"

    consumer_key: Mapped[str] = mapped_column(String(128), primary_key=True)
    last_event_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), nullable=True
    )
    last_event_created_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    cursor_metadata: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
