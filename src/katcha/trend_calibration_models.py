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


class TrendOutcomeAttribution(Base):
    __tablename__ = "trend_outcome_attributions"
    __table_args__ = (
        UniqueConstraint("analytics_snapshot_id"),
    )

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
    trend_opportunity_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("trend_opportunities.id"),
        nullable=True,
        index=True,
    )
    status: Mapped[str] = mapped_column(String(32), index=True)
    reason: Mapped[str] = mapped_column(Text)
    source_kind: Mapped[str] = mapped_column(String(32), index=True)
    attribution_metadata: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )


class TrendOpportunityOutcome(Base):
    __tablename__ = "trend_opportunity_outcomes"
    __table_args__ = (
        UniqueConstraint("analytics_snapshot_id"),
        CheckConstraint(
            "age_bucket_hours > 0",
            name="ck_trend_outcome_age_bucket_positive",
        ),
        CheckConstraint(
            "predicted_score >= 0 AND predicted_score <= 1",
            name="ck_trend_outcome_predicted_score",
        ),
        CheckConstraint(
            "predicted_confidence >= 0 AND predicted_confidence <= 1",
            name="ck_trend_outcome_predicted_confidence",
        ),
        CheckConstraint(
            "observed_outcome_score >= 0 AND observed_outcome_score <= 1",
            name="ck_trend_outcome_observed_score",
        ),
        CheckConstraint(
            "baseline_sample_count >= 0",
            name="ck_trend_outcome_baseline_samples_nonnegative",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    channel_profile_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("channel_profiles.id"), index=True
    )
    trend_opportunity_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("trend_opportunities.id"), index=True
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
    age_bucket_hours: Mapped[int] = mapped_column(Integer, index=True)
    opportunity_created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), index=True
    )
    published_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    sampled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    lead_time_hours: Mapped[Decimal] = mapped_column(Numeric(14, 4))
    predicted_score: Mapped[Decimal] = mapped_column(Numeric(8, 6))
    predicted_confidence: Mapped[Decimal] = mapped_column(Numeric(8, 6))
    predicted_lifecycle: Mapped[str] = mapped_column(String(32), index=True)
    prediction_components: Mapped[dict[str, float]] = mapped_column(JSON, default=dict)
    observed_outcome_score: Mapped[Decimal] = mapped_column(Numeric(8, 6), index=True)
    baseline_sample_count: Mapped[int] = mapped_column(Integer, default=0)
    baseline_outcome_score: Mapped[Decimal | None] = mapped_column(
        Numeric(8, 6), nullable=True
    )
    lift_ratio: Mapped[Decimal | None] = mapped_column(Numeric(14, 6), nullable=True)
    realized_breakout: Mapped[bool | None] = mapped_column(
        Boolean, nullable=True, index=True
    )
    outcome_metrics: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )


class TrendCalibrationSnapshot(Base):
    __tablename__ = "trend_calibration_snapshots"
    __table_args__ = (
        UniqueConstraint("channel_profile_id", "version"),
        UniqueConstraint("channel_profile_id", "run_key"),
        CheckConstraint(
            "sample_count >= 0",
            name="ck_trend_calibration_sample_count_nonnegative",
        ),
        CheckConstraint(
            "training_sample_count >= 0",
            name="ck_trend_calibration_training_count_nonnegative",
        ),
        CheckConstraint(
            "validation_sample_count >= 0",
            name="ck_trend_calibration_validation_count_nonnegative",
        ),
        CheckConstraint(
            "blend_ratio >= 0 AND blend_ratio <= 1",
            name="ck_trend_calibration_blend_ratio",
        ),
        CheckConstraint(
            "confidence >= 0 AND confidence <= 1",
            name="ck_trend_calibration_confidence",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    channel_profile_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("channel_profiles.id"), index=True
    )
    version: Mapped[int] = mapped_column(Integer)
    run_key: Mapped[str] = mapped_column(String(160), index=True)
    algorithm: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(32), index=True)
    training_cutoff: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), index=True
    )
    sample_count: Mapped[int] = mapped_column(Integer)
    training_sample_count: Mapped[int] = mapped_column(Integer)
    validation_sample_count: Mapped[int] = mapped_column(Integer)
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
    calibration_metrics: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
