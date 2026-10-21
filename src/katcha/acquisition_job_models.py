"""Control-plane leases for remote execution of existing acquisition receipts."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from katcha.db import Base


class AcquisitionJob(Base):
    __tablename__ = "acquisition_jobs"
    __mapper_args__ = {"eager_defaults": True}
    __table_args__ = (
        UniqueConstraint("receipt_id", name="uq_acquisition_job_receipt"),
        ForeignKeyConstraint(
            ["receipt_id", "channel_profile_id"],
            ["acquisition_receipts.id", "acquisition_receipts.channel_profile_id"],
            name="fk_acquisition_job_receipt_channel",
        ),
        CheckConstraint(
            "status IN ('queued', 'leased', 'awaiting_validation', 'failed', "
            "'cancelled', 'recovery_required', 'completed')",
            name="ck_acquisition_job_status",
        ),
        CheckConstraint("generation >= 0", name="ck_acquisition_job_generation"),
        CheckConstraint("max_bytes > 0", name="ck_acquisition_job_max_bytes"),
        CheckConstraint(
            "bytes_received >= 0 AND bytes_received <= max_bytes",
            name="ck_acquisition_job_progress",
        ),
        CheckConstraint("heartbeat_sequence >= 0", name="ck_acquisition_job_sequence"),
        CheckConstraint(
            "status != 'leased' OR (worker_principal IS NOT NULL AND claim_id IS NOT NULL "
            "AND lease_expires_at IS NOT NULL AND generation > 0)",
            name="ck_acquisition_job_lease",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    receipt_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    channel_profile_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="queued", index=True)
    generation: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    media_type: Mapped[str] = mapped_column(String(16), nullable=False, default="video")
    source_item_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    bytes_received: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    heartbeat_sequence: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    worker_principal: Mapped[str | None] = mapped_column(String(64), nullable=True)
    claim_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    phase: Mapped[str] = mapped_column(String(32), nullable=False, default="queued")
    pending_result: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    failure_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    @property
    def media_verified(self) -> bool:
        return self.status == "completed"


class AcquisitionTransfer(Base):
    """Server-owned multipart cursor. Workers never receive bucket credentials."""

    __tablename__ = "acquisition_transfers"
    __table_args__ = (
        ForeignKeyConstraint(["job_id"], ["acquisition_jobs.id"]),
        CheckConstraint("size_bytes > 0", name="ck_acquisition_transfer_size"),
    )

    job_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    generation: Mapped[int] = mapped_column(Integer, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    upload_id: Mapped[str] = mapped_column(Text, nullable=False)
    # Part MD5 and length are frozen before a URL is issued. S3 checks Content-MD5.
    parts: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    completed: Mapped[bool] = mapped_column(default=False, nullable=False)
