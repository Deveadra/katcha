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
from katcha.domain import (
    AudioRightsStatus,
    DiscoveryCandidateStatus,
    DiscoveryRunStatus,
    GateStatus,
    RightsBasis,
    RightsLane,
)


class DiscoveryRun(Base):
    __tablename__ = "discovery_runs"
    __table_args__ = (UniqueConstraint("adapter_key", "run_key"),)

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    adapter_key: Mapped[str] = mapped_column(String(64), index=True)
    adapter_version: Mapped[str] = mapped_column(String(64))
    run_key: Mapped[str] = mapped_column(String(160), index=True)
    status: Mapped[str] = mapped_column(
        String(32), default=DiscoveryRunStatus.QUEUED.value, index=True
    )
    query: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    cursor: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    run_metadata: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class DiscoveryCandidate(Base):
    __tablename__ = "discovery_candidates"
    __table_args__ = (
        UniqueConstraint("adapter_key", "external_id"),
        UniqueConstraint("source_item_id"),
        CheckConstraint(
            "provenance_confidence >= 0 AND provenance_confidence <= 1",
            name="ck_discovery_provenance_confidence",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    discovery_run_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("discovery_runs.id"),
        nullable=True,
        index=True,
    )
    source_item_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("source_items.id"),
        nullable=True,
        index=True,
    )
    adapter_key: Mapped[str] = mapped_column(String(64), index=True)
    external_id: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    source_url: Mapped[str] = mapped_column(Text)
    canonical_url: Mapped[str] = mapped_column(Text, unique=True)
    platform: Mapped[str] = mapped_column(String(32), index=True)
    status: Mapped[str] = mapped_column(
        String(32), default=DiscoveryCandidateStatus.DISCOVERED.value, index=True
    )
    title: Mapped[str | None] = mapped_column(Text, nullable=True)
    creator: Mapped[str | None] = mapped_column(Text, nullable=True)
    creator_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    provenance_confidence: Mapped[Decimal] = mapped_column(
        Numeric(8, 6), default=Decimal("0")
    )
    provenance_claims: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    candidate_metadata: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    discovered_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class DiscoveryObservation(Base):
    __tablename__ = "discovery_observations"
    __table_args__ = (
        UniqueConstraint("discovery_run_id", "discovery_candidate_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    discovery_run_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("discovery_runs.id"), index=True
    )
    discovery_candidate_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("discovery_candidates.id"), index=True
    )
    external_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    observation_metadata: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )


class RightsAssessment(Base):
    __tablename__ = "rights_assessments"
    __table_args__ = (
        UniqueConstraint("discovery_candidate_id", "version"),
        CheckConstraint("version > 0", name="ck_rights_assessment_version_positive"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    discovery_candidate_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("discovery_candidates.id"),
        index=True,
    )
    version: Mapped[int] = mapped_column(Integer)
    rights_basis: Mapped[str] = mapped_column(
        String(64), default=RightsBasis.UNKNOWN.value, index=True
    )
    rights_lane: Mapped[str] = mapped_column(
        String(16), default=RightsLane.YELLOW.value, index=True
    )
    rights_gate: Mapped[str] = mapped_column(
        String(32), default=GateStatus.REVIEW_REQUIRED.value, index=True
    )
    audio_status: Mapped[str] = mapped_column(
        String(32), default=AudioRightsStatus.REVIEW_REQUIRED.value, index=True
    )
    originality_gate: Mapped[str] = mapped_column(
        String(32), default=GateStatus.REVIEW_REQUIRED.value, index=True
    )
    production_eligible: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    operator_authorized: Mapped[bool] = mapped_column(Boolean, default=False)
    risk_flags: Mapped[list[str]] = mapped_column(JSON, default=list)
    fair_use_factors: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    assessment_metadata: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    actor: Mapped[str] = mapped_column(String(128), default="system")
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )


class RightsEvidence(Base):
    __tablename__ = "rights_evidence"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    rights_assessment_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("rights_assessments.id"),
        index=True,
    )
    evidence_type: Mapped[str] = mapped_column(String(64), index=True)
    source_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    snapshot_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    content_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    terms_version: Mapped[str | None] = mapped_column(String(128), nullable=True)
    evidence_metadata: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    captured_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
