from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from katcha.db import Base


class PublicationPackagingVariant(Base):
    __tablename__ = "publication_packaging_variants"
    __table_args__ = (
        UniqueConstraint(
            "publication_id",
            "variant_key",
            "version",
            name="uq_packaging_variant_publication_key_version",
        ),
        CheckConstraint(
            "version > 0",
            name="ck_packaging_variant_version_positive",
        ),
        CheckConstraint(
            "(thumbnail_storage_key IS NULL "
            "AND thumbnail_content_type IS NULL "
            "AND thumbnail_size_bytes IS NULL "
            "AND thumbnail_sha256 IS NULL) OR "
            "(thumbnail_storage_key IS NOT NULL "
            "AND thumbnail_content_type IS NOT NULL "
            "AND thumbnail_size_bytes IS NOT NULL "
            "AND thumbnail_sha256 IS NOT NULL)",
            name="ck_packaging_variant_thumbnail_metadata_complete",
        ),
        CheckConstraint(
            "thumbnail_size_bytes IS NULL OR thumbnail_size_bytes > 0",
            name="ck_packaging_variant_thumbnail_size_positive",
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
    variant_key: Mapped[str] = mapped_column(String(128))
    version: Mapped[int] = mapped_column(Integer)
    title: Mapped[str] = mapped_column(String(100))
    description: Mapped[str] = mapped_column(Text, default="")
    thumbnail_storage_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    thumbnail_content_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    thumbnail_size_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    thumbnail_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_by: Mapped[str] = mapped_column(String(128), default="operator")
    variant_metadata: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        index=True,
    )


class PublicationPackagingActivation(Base):
    __tablename__ = "publication_packaging_activations"
    __table_args__ = (
        UniqueConstraint(
            "publication_id",
            "activation_key",
            name="uq_packaging_activation_publication_key",
        ),
        CheckConstraint(
            "status IN ('queued', 'running', 'applied', 'failed')",
            name="ck_packaging_activation_status",
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
    variant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("publication_packaging_variants.id"),
        index=True,
    )
    activation_key: Mapped[str] = mapped_column(String(160))
    workflow_id: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    status: Mapped[str] = mapped_column(String(32), default="queued", index=True)
    stage: Mapped[str] = mapped_column(String(64), default="queued")
    youtube_video_id: Mapped[str] = mapped_column(String(64), index=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    activation_metadata: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        index=True,
    )
    applied_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )


class PackagingCandidateGeneration(Base):
    __tablename__ = "packaging_candidate_generations"
    __table_args__ = (
        UniqueConstraint(
            "publication_id",
            "generation_key",
            name="uq_packaging_candidate_generation_publication_key",
        ),
        CheckConstraint(
            "status IN ('queued', 'running', 'completed', 'failed', 'ambiguous')",
            name="ck_packaging_candidate_generation_status",
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
    generation_key: Mapped[str] = mapped_column(String(160))
    status: Mapped[str] = mapped_column(String(32), default="queued", index=True)
    stage: Mapped[str] = mapped_column(String(64), default="queued")
    prompt_version: Mapped[str] = mapped_column(
        String(64), default="packaging-candidates-v1"
    )
    context_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    provider: Mapped[str | None] = mapped_column(String(32), nullable=True)
    model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    candidate_payload: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    accepted_variant_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    generation_metadata: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        index=True,
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
