from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    JSON,
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


class EditBlueprintPerformanceSnapshot(Base):
    __tablename__ = "edit_blueprint_performance_snapshots"
    __table_args__ = (
        UniqueConstraint("channel_profile_id", "version"),
        UniqueConstraint("channel_profile_id", "run_key"),
        CheckConstraint(
            "publication_count >= 0",
            name="ck_edit_perf_publication_count_nonnegative",
        ),
        CheckConstraint(
            "blueprint_group_count >= 0",
            name="ck_edit_perf_group_count_nonnegative",
        ),
        CheckConstraint(
            "revenue_covered_publications >= 0",
            name="ck_edit_perf_revenue_covered_nonnegative",
        ),
        CheckConstraint(
            "monetary_coverage >= 0 AND monetary_coverage <= 1",
            name="ck_edit_perf_monetary_coverage",
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
    publication_count: Mapped[int] = mapped_column(Integer, default=0)
    blueprint_group_count: Mapped[int] = mapped_column(Integer, default=0)
    revenue_covered_publications: Mapped[int] = mapped_column(Integer, default=0)
    monetary_coverage: Mapped[Decimal] = mapped_column(
        Numeric(8, 6), default=Decimal("0")
    )
    aggregate_metrics: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    comparison_status: Mapped[str] = mapped_column(
        String(64), default="insufficient_data", index=True
    )
    comparison_summary: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    sample_window_start: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    sample_window_end: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
