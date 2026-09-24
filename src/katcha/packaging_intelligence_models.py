from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    JSON,
    CheckConstraint,
    Date,
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


class PackagingVariantPerformanceWindow(Base):
    __tablename__ = "packaging_variant_performance_windows"
    __table_args__ = (
        UniqueConstraint(
            "packaging_variant_id",
            "evidence_key",
            name="uq_packaging_window_variant_evidence",
        ),
        CheckConstraint(
            "maturity_days > 0",
            name="ck_packaging_window_maturity_positive",
        ),
        CheckConstraint(
            "window_end >= window_start",
            name="ck_packaging_window_dates",
        ),
        CheckConstraint(
            "impressions IS NULL OR impressions >= 0",
            name="ck_packaging_window_impressions_nonnegative",
        ),
        CheckConstraint(
            "ctr IS NULL OR (ctr >= 0 AND ctr <= 1)",
            name="ck_packaging_window_ctr",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    publication_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("publications.id"),
        index=True,
    )
    packaging_variant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("publication_packaging_variants.id"),
        index=True,
    )
    evidence_key: Mapped[str] = mapped_column(String(64), index=True)
    maturity_days: Mapped[int] = mapped_column(Integer, index=True)
    window_start: Mapped[date] = mapped_column(Date, index=True)
    window_end: Mapped[date] = mapped_column(Date, index=True)
    impressions: Mapped[int | None] = mapped_column(nullable=True)
    ctr: Mapped[Decimal | None] = mapped_column(Numeric(12, 8), nullable=True)
    views: Mapped[int | None] = mapped_column(nullable=True)
    average_view_percentage: Mapped[Decimal | None] = mapped_column(
        Numeric(12, 6),
        nullable=True,
    )
    retention_50: Mapped[Decimal | None] = mapped_column(
        Numeric(12, 8),
        nullable=True,
    )
    interval_revenue_usd: Mapped[Decimal | None] = mapped_column(
        Numeric(18, 8),
        nullable=True,
    )
    publication_cost_usd: Mapped[Decimal] = mapped_column(
        Numeric(18, 8),
        default=Decimal("0"),
    )
    publication_revenue_usd: Mapped[Decimal | None] = mapped_column(
        Numeric(18, 8),
        nullable=True,
    )
    publication_contribution_margin_usd: Mapped[Decimal | None] = mapped_column(
        Numeric(18, 8),
        nullable=True,
    )
    evidence_status: Mapped[str] = mapped_column(
        String(64),
        default="measured",
        index=True,
    )
    evidence_metadata: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )


class PackagingIntelligenceSnapshot(Base):
    __tablename__ = "packaging_intelligence_snapshots"
    __table_args__ = (
        UniqueConstraint("channel_profile_id", "version"),
        UniqueConstraint("channel_profile_id", "run_key"),
        CheckConstraint(
            "maturity_days > 0",
            name="ck_packaging_intelligence_maturity_positive",
        ),
        CheckConstraint(
            "publication_count >= 0",
            name="ck_packaging_intelligence_publications_nonnegative",
        ),
        CheckConstraint(
            "variant_window_count >= 0",
            name="ck_packaging_intelligence_windows_nonnegative",
        ),
        CheckConstraint(
            "recommendation_count >= 0",
            name="ck_packaging_intelligence_recommendations_nonnegative",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    channel_profile_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("channel_profiles.id"),
        index=True,
    )
    version: Mapped[int] = mapped_column(Integer)
    run_key: Mapped[str] = mapped_column(String(160), index=True)
    maturity_days: Mapped[int] = mapped_column(Integer, default=7, index=True)
    publication_count: Mapped[int] = mapped_column(Integer, default=0)
    variant_window_count: Mapped[int] = mapped_column(Integer, default=0)
    recommendation_count: Mapped[int] = mapped_column(Integer, default=0)
    recommendation_status: Mapped[str] = mapped_column(
        String(64),
        default="insufficient_data",
        index=True,
    )
    variant_metrics: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    recommendations: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    validation_metrics: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    policy_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    sample_window_start: Mapped[date | None] = mapped_column(Date, nullable=True)
    sample_window_end: Mapped[date | None] = mapped_column(Date, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )
