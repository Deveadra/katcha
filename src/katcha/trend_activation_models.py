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


class TrendActivationPolicyVersion(Base):
    __tablename__ = "trend_activation_policy_versions"
    __table_args__ = (
        UniqueConstraint("channel_profile_id", "version"),
        CheckConstraint(
            "min_opportunity_score >= 0 AND min_opportunity_score <= 1",
            name="ck_trend_activation_policy_min_score",
        ),
        CheckConstraint(
            "min_confidence >= 0 AND min_confidence <= 1",
            name="ck_trend_activation_policy_min_confidence",
        ),
        CheckConstraint(
            "min_calibrated_score IS NULL OR "
            "(min_calibrated_score >= 0 AND min_calibrated_score <= 1)",
            name="ck_trend_activation_policy_min_calibrated",
        ),
        CheckConstraint(
            "min_rights_readiness >= 0 AND min_rights_readiness <= 1",
            name="ck_trend_activation_policy_rights_readiness",
        ),
        CheckConstraint(
            "min_lead_time_minutes >= 0",
            name="ck_trend_activation_policy_lead_time",
        ),
        CheckConstraint(
            "max_activations_per_day >= 0",
            name="ck_trend_activation_policy_daily_cap",
        ),
        CheckConstraint(
            "cooldown_minutes >= 0",
            name="ck_trend_activation_policy_cooldown",
        ),
        CheckConstraint(
            "max_backlog >= 0",
            name="ck_trend_activation_policy_backlog",
        ),
        CheckConstraint(
            "min_budget_headroom_usd >= 0",
            name="ck_trend_activation_policy_budget_headroom",
        ),
        CheckConstraint(
            "max_opportunities_per_run > 0",
            name="ck_trend_activation_policy_max_inspected",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    channel_profile_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("channel_profiles.id"), index=True
    )
    version: Mapped[int] = mapped_column(Integer)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    min_opportunity_score: Mapped[Decimal] = mapped_column(
        Numeric(8, 6), default=Decimal("0.65")
    )
    min_confidence: Mapped[Decimal] = mapped_column(
        Numeric(8, 6), default=Decimal("0.55")
    )
    min_calibrated_score: Mapped[Decimal | None] = mapped_column(
        Numeric(8, 6), nullable=True
    )
    min_rights_readiness: Mapped[Decimal] = mapped_column(
        Numeric(8, 6), default=Decimal("0.50")
    )
    min_lead_time_minutes: Mapped[int] = mapped_column(Integer, default=120)
    max_activations_per_day: Mapped[int] = mapped_column(Integer, default=3)
    cooldown_minutes: Mapped[int] = mapped_column(Integer, default=120)
    max_backlog: Mapped[int] = mapped_column(Integer, default=5)
    min_budget_headroom_usd: Mapped[Decimal] = mapped_column(
        Numeric(14, 4), default=Decimal("2.00")
    )
    max_opportunities_per_run: Mapped[int] = mapped_column(Integer, default=25)
    policy_metadata: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )


class TrendActivationRun(Base):
    __tablename__ = "trend_activation_runs"
    __table_args__ = (
        UniqueConstraint("channel_profile_id", "run_key"),
        CheckConstraint("inspected_count >= 0", name="ck_trend_activation_run_inspected"),
        CheckConstraint("activated_count >= 0", name="ck_trend_activation_run_activated"),
        CheckConstraint("deferred_count >= 0", name="ck_trend_activation_run_deferred"),
        CheckConstraint("skipped_count >= 0", name="ck_trend_activation_run_skipped"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    channel_profile_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("channel_profiles.id"), index=True
    )
    policy_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    run_key: Mapped[str] = mapped_column(String(160), index=True)
    status: Mapped[str] = mapped_column(String(32), default="running", index=True)
    inspected_count: Mapped[int] = mapped_column(Integer, default=0)
    activated_count: Mapped[int] = mapped_column(Integer, default=0)
    deferred_count: Mapped[int] = mapped_column(Integer, default=0)
    skipped_count: Mapped[int] = mapped_column(Integer, default=0)
    run_metadata: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class TrendActivationDecision(Base):
    __tablename__ = "trend_activation_decisions"
    __table_args__ = (
        UniqueConstraint("activation_run_id", "trend_opportunity_id"),
        CheckConstraint(
            "opportunity_score >= 0 AND opportunity_score <= 1",
            name="ck_trend_activation_decision_score",
        ),
        CheckConstraint(
            "confidence >= 0 AND confidence <= 1",
            name="ck_trend_activation_decision_confidence",
        ),
        CheckConstraint(
            "calibrated_score IS NULL OR "
            "(calibrated_score >= 0 AND calibrated_score <= 1)",
            name="ck_trend_activation_decision_calibrated",
        ),
        CheckConstraint(
            "rights_readiness >= 0 AND rights_readiness <= 1",
            name="ck_trend_activation_decision_rights_readiness",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    activation_run_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("trend_activation_runs.id"), index=True
    )
    channel_profile_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("channel_profiles.id"), index=True
    )
    trend_opportunity_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("trend_opportunities.id"), index=True
    )
    trend_evidence_packet_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("trend_evidence_packets.id"), nullable=True, index=True
    )
    short_episode_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("short_episodes.id"), nullable=True, index=True
    )
    activation_key: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    decision: Mapped[str] = mapped_column(String(48), index=True)
    reason: Mapped[str] = mapped_column(Text)
    opportunity_score: Mapped[Decimal] = mapped_column(Numeric(8, 6))
    confidence: Mapped[Decimal] = mapped_column(Numeric(8, 6))
    calibrated_score: Mapped[Decimal | None] = mapped_column(Numeric(8, 6), nullable=True)
    rights_readiness: Mapped[Decimal] = mapped_column(Numeric(8, 6))
    remaining_lead_minutes: Mapped[int] = mapped_column(Integer)
    decision_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
