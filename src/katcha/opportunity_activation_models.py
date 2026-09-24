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


class OpportunityActivationPolicyVersion(Base):
    __tablename__ = "opportunity_activation_policy_versions"
    __table_args__ = (
        UniqueConstraint("channel_profile_id", "version"),
        CheckConstraint(
            "min_opportunity_score >= 0 AND min_opportunity_score <= 1",
            name="ck_activation_policy_opportunity_score",
        ),
        CheckConstraint(
            "min_confidence >= 0 AND min_confidence <= 1",
            name="ck_activation_policy_confidence",
        ),
        CheckConstraint(
            "min_calibrated_score IS NULL OR "
            "(min_calibrated_score >= 0 AND min_calibrated_score <= 1)",
            name="ck_activation_policy_calibrated_score",
        ),
        CheckConstraint(
            "min_rights_readiness >= 0 AND min_rights_readiness <= 1",
            name="ck_activation_policy_rights_readiness",
        ),
        CheckConstraint(
            "min_lead_minutes >= 0",
            name="ck_activation_policy_lead_minutes",
        ),
        CheckConstraint(
            "max_activations_per_day >= 0",
            name="ck_activation_policy_daily_limit",
        ),
        CheckConstraint(
            "cooldown_minutes >= 0",
            name="ck_activation_policy_cooldown",
        ),
        CheckConstraint(
            "max_planned_backlog >= 0",
            name="ck_activation_policy_backlog",
        ),
        CheckConstraint(
            "min_budget_headroom_usd >= 0",
            name="ck_activation_policy_budget_headroom",
        ),
        CheckConstraint(
            "max_inspected_per_run > 0",
            name="ck_activation_policy_inspected_positive",
        ),
        CheckConstraint(
            "item_count IS NULL OR item_count > 0",
            name="ck_activation_policy_item_count_positive",
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
        Numeric(8, 6), default=Decimal("0.60")
    )
    min_calibrated_score: Mapped[Decimal | None] = mapped_column(
        Numeric(8, 6), nullable=True
    )
    min_rights_readiness: Mapped[Decimal] = mapped_column(
        Numeric(8, 6), default=Decimal("0")
    )
    min_lead_minutes: Mapped[int] = mapped_column(Integer, default=60)
    max_activations_per_day: Mapped[int] = mapped_column(Integer, default=3)
    cooldown_minutes: Mapped[int] = mapped_column(Integer, default=30)
    max_planned_backlog: Mapped[int] = mapped_column(Integer, default=5)
    min_budget_headroom_usd: Mapped[Decimal] = mapped_column(
        Numeric(14, 4), default=Decimal("0.05")
    )
    max_inspected_per_run: Mapped[int] = mapped_column(Integer, default=20)
    item_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    policy_metadata: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class OpportunityActivationRun(Base):
    __tablename__ = "opportunity_activation_runs"
    __table_args__ = (
        UniqueConstraint("channel_profile_id", "run_key"),
        CheckConstraint("scanned_count >= 0", name="ck_activation_run_scanned_nonnegative"),
        CheckConstraint(
            "activated_count >= 0", name="ck_activation_run_activated_nonnegative"
        ),
        CheckConstraint("skipped_count >= 0", name="ck_activation_run_skipped_nonnegative"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    channel_profile_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("channel_profiles.id"), index=True
    )
    run_key: Mapped[str] = mapped_column(String(160), index=True)
    policy_version: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(32), default="running", index=True)
    scanned_count: Mapped[int] = mapped_column(Integer, default=0)
    activated_count: Mapped[int] = mapped_column(Integer, default=0)
    skipped_count: Mapped[int] = mapped_column(Integer, default=0)
    run_metadata: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class OpportunityActivationDecision(Base):
    __tablename__ = "opportunity_activation_decisions"
    __table_args__ = (
        UniqueConstraint("activation_run_id", "trend_opportunity_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    activation_run_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("opportunity_activation_runs.id"), index=True
    )
    channel_profile_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("channel_profiles.id"), index=True
    )
    trend_opportunity_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("trend_opportunities.id"), index=True
    )
    short_episode_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("short_episodes.id"), nullable=True, index=True
    )
    outcome: Mapped[str] = mapped_column(String(32), index=True)
    reason: Mapped[str] = mapped_column(String(128), index=True)
    activation_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    decision_metadata: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
