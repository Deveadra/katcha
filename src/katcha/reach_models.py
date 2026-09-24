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
            "status IN ('registered', 'create_started', 'active', 'failed')",
            name="ck_youtube_reach_job_status",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    youtube_connection_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("youtube_connections.id"), index=True
    )
    report_type_id: Mapped[str] = mapped_column(String(96), index=True)
    provider_job_id: Mapped[str | None] = mapped_column(String(160), nullable=True, index=True)
    status: Mapped[str] = mapped_column(String(32), default="registered", index=True)
    stage: Mapped[str] = mapped_column(String(64), default="registered")
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    job_metadata: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class YouTubeReachReportImport(Base):
    __tablename__ = "youtube_reach_report_imports"
    __table_args__ = (
        UniqueConstraint(
            "youtube_connection_id",
            "provider_report_id",
            name="uq_youtube_reach_report_connection_provider",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    youtube_connection_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("youtube_connections.id"), index=True
    )
    reach_job_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("youtube_reach_reporting_jobs.id"), index=True
    )
    provider_report_id: Mapped[str] = mapped_column(String(200), index=True)
    report_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    report_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    provider_created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    payload_sha256: Mapped[str] = mapped_column(String(64))
    row_count: Mapped[int] = mapped_column(BigInteger, default=0)
    import_metadata: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    imported_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class PublicationReachObservation(Base):
    __tablename__ = "publication_reach_observations"
    __table_args__ = (
        UniqueConstraint(
            "publication_id",
            "report_date",
            name="uq_publication_reach_observation_day",
        ),
        CheckConstraint(
            "attribution_status IN ('variant', 'legacy', 'mixed')",
            name="ck_publication_reach_attribution_status",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    publication_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("publications.id"), index=True
    )
    report_import_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("youtube_reach_report_imports.id"), index=True
    )
    report_date: Mapped[date] = mapped_column(Date, index=True)
    impressions: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    ctr: Mapped[Decimal | None] = mapped_column(Numeric(12, 8), nullable=True)
    attribution_status: Mapped[str] = mapped_column(String(32), index=True)
    packaging_variant_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("publication_packaging_variants.id"), nullable=True, index=True
    )
    attribution_metadata: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    raw_row: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
