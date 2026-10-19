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
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from katcha.db import Base


class AcquisitionJob(Base):
    __tablename__ = "acquisition_jobs"
    __table_args__ = (
        UniqueConstraint("receipt_id", name="uq_acquisition_job_receipt"),
        ForeignKeyConstraint(
            ["receipt_id", "channel_profile_id"],
            ["acquisition_receipts.id", "acquisition_receipts.channel_profile_id"],
            name="fk_acquisition_job_receipt_channel",
        ),
        CheckConstraint(
            "status IN ('queued', 'leased', 'awaiting_validation', 'failed', "
            "'cancelled', 'recovery_required')",
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
