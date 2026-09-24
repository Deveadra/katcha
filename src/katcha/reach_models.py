from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    CheckConstraint,
    Date,
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


class YouTubeReachReportingJob(Base):
    __tablename__ = "youtube_reach_reporting_jobs"
    __table_args__ = (
        UniqueConstraint(
            "youtube_connection_id",
            "report_type_id",
            name="uq_youtube_reach_job_connection_type",
        ),
        CheckConstraint(
            "status IN ('pending', 'active', 'failed')",
            name="ck_youtube_reach_job_status",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    youtube_connection_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("youtube_connections.id"),
        index=True,
    )
    report_type_id: Mapped[str] = mapped_column(
        String(128), default="channel_reach_basic_a1"
    )
    provider_job_id: Mapped[str | None] = mapped_column(
        String(255), nullable=True, unique=True, index=True
    )
    status: Mapped[str] = mapped_column(String(32), default="pending", index=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    job_metadata: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class YouTubeReachReportImport(Base):
    __tablename__ = "youtube_reach_report_imports"
    __table_args__ = (
        UniqueConstraint(
            "youtube_connection_id",
            "provider_report_id",
            name="uq_youtube_reach_report_connection_report",
        ),
        CheckConstraint(
            "status IN ('imported', 'failed')",
            name="ck_youtube_reach_report_import_status",
        ),
        CheckConstraint(
            "row_count >= 0",
            name="ck_youtube_reach_report_row_count_nonnegative",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    reporting_job_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("youtube_reach_reporting_jobs.id"),
        index=True,
    )
    youtube_connection_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("youtube_connections.id"),
        index=True,
    )
    provider_report_id: Mapped[str] = mapped_column(String(255), index=True)
    report_start_time: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    report_end_time: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    provider_create_time: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    payload_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    row_count: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(32), default="imported", index=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    report_metadata: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    imported_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )


class PublicationReachObservation(Base):
    __tablename__ = "publication_reach_observations"
    __table_args__ = (
        UniqueConstraint(
            "report_import_id",
            "publication_id",
            "report_date",
            name="uq_publication_reach_import_publication_date",
        ),
        CheckConstraint(
            "thumbnail_impressions IS NULL OR thumbnail_impressions >= 0",
            name="ck_publication_reach_impressions_nonnegative",
        ),
        CheckConstraint(
            "thumbnail_ctr IS NULL OR (thumbnail_ctr >= 0 AND thumbnail_ctr <= 1)",
            name="ck_publication_reach_ctr_fraction",
        ),
        CheckConstraint(
            "attribution_status IN "
            "('variant', 'legacy_baseline', 'mixed_variant_day')",
            name="ck_publication_reach_attribution_status",
        ),
        CheckConstraint(
            "(attribution_status = 'variant' AND packaging_variant_id IS NOT NULL) OR "
            "(attribution_status != 'variant' AND packaging_variant_id IS NULL)",
            name="ck_publication_reach_variant_attribution",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    report_import_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("youtube_reach_report_imports.id"),
        index=True,
    )
    publication_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("publications.id"), index=True
    )
    packaging_variant_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("publication_packaging_variants.id"),
        nullable=True,
        index=True,
    )
    youtube_video_id: Mapped[str] = mapped_column(String(64), index=True)
    report_date: Mapped[date] = mapped_column(Date, index=True)
    thumbnail_impressions: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    thumbnail_ctr: Mapped[Decimal | None] = mapped_column(
        Numeric(12, 8), nullable=True
    )
    attribution_status: Mapped[str] = mapped_column(String(32), index=True)
    raw_row: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
