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
    Text,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from katcha.db import Base


class ChannelTrendWatchVersion(Base):
    __tablename__ = "channel_trend_watch_versions"
    __table_args__ = (
        UniqueConstraint("channel_profile_id", "version"),
        CheckConstraint(
            "freshness_horizon_hours > 0",
            name="ck_trend_watch_freshness_positive",
        ),
        CheckConstraint(
            "min_confidence >= 0 AND min_confidence <= 1",
            name="ck_trend_watch_min_confidence",
        ),
        CheckConstraint(
            "opportunity_threshold >= 0 AND opportunity_threshold <= 1",
            name="ck_trend_watch_opportunity_threshold",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    channel_profile_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("channel_profiles.id"), index=True
    )
    version: Mapped[int] = mapped_column(Integer)
    interests: Mapped[list[str]] = mapped_column(JSON, default=list)
    excluded_terms: Mapped[list[str]] = mapped_column(JSON, default=list)
    entities: Mapped[list[str]] = mapped_column(JSON, default=list)
    platforms: Mapped[list[str]] = mapped_column(JSON, default=list)
    languages: Mapped[list[str]] = mapped_column(JSON, default=list)
    regions: Mapped[list[str]] = mapped_column(JSON, default=list)
    source_weights: Mapped[dict[str, float]] = mapped_column(JSON, default=dict)
    freshness_horizon_hours: Mapped[int] = mapped_column(Integer, default=72)
    min_confidence: Mapped[Decimal] = mapped_column(
        Numeric(8, 6), default=Decimal("0.45")
    )
    opportunity_threshold: Mapped[Decimal] = mapped_column(
        Numeric(8, 6), default=Decimal("0.55")
    )
    watch_metadata: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )


class TrendTopic(Base):
    __tablename__ = "trend_topics"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    topic_key: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    display_name: Mapped[str] = mapped_column(Text)
    aliases: Mapped[list[str]] = mapped_column(JSON, default=list)
    tags: Mapped[list[str]] = mapped_column(JSON, default=list)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    topic_metadata: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class TrendSignal(Base):
    __tablename__ = "trend_signals"
    __table_args__ = (
        UniqueConstraint("provider_key", "external_id", "observation_key"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    provider_key: Mapped[str] = mapped_column(String(64), index=True)
    external_id: Mapped[str] = mapped_column(String(512), index=True)
    observation_key: Mapped[str] = mapped_column(String(128))
    canonical_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_kind: Mapped[str] = mapped_column(String(64), index=True)
    source_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    independence_key: Mapped[str] = mapped_column(String(512), index=True)
    title: Mapped[str | None] = mapped_column(Text, nullable=True)
    body_excerpt: Mapped[str | None] = mapped_column(Text, nullable=True)
    author: Mapped[str | None] = mapped_column(String(255), nullable=True)
    community: Mapped[str | None] = mapped_column(String(255), nullable=True)
    language: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    region: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    published_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    metrics: Mapped[dict[str, float]] = mapped_column(JSON, default=dict)
    media_refs: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    content_fingerprint: Mapped[str | None] = mapped_column(
        String(128), nullable=True, index=True
    )
    signal_metadata: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class TrendTopicSignal(Base):
    __tablename__ = "trend_topic_signals"
    __table_args__ = (
        UniqueConstraint("trend_topic_id", "trend_signal_id"),
        CheckConstraint(
            "match_confidence >= 0 AND match_confidence <= 1",
            name="ck_trend_topic_signal_confidence",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    trend_topic_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("trend_topics.id"), index=True
    )
    trend_signal_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("trend_signals.id"), index=True
    )
    match_confidence: Mapped[Decimal] = mapped_column(
        Numeric(8, 6), default=Decimal("1")
    )
    match_reasons: Mapped[list[str]] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class TrendOpportunity(Base):
    __tablename__ = "trend_opportunities"
    __table_args__ = (
        UniqueConstraint("channel_profile_id", "trend_topic_id", "run_key"),
        CheckConstraint(
            "opportunity_score >= 0 AND opportunity_score <= 1",
            name="ck_trend_opportunity_score",
        ),
        CheckConstraint(
            "confidence >= 0 AND confidence <= 1",
            name="ck_trend_opportunity_confidence",
        ),
        CheckConstraint(
            "prediction_horizon_hours > 0",
            name="ck_trend_prediction_horizon_positive",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    channel_profile_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("channel_profiles.id"), index=True
    )
    trend_topic_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("trend_topics.id"), index=True
    )
    watch_version: Mapped[int] = mapped_column(Integer)
    run_key: Mapped[str] = mapped_column(String(160), index=True)
    lifecycle: Mapped[str] = mapped_column(String(32), index=True)
    opportunity_score: Mapped[Decimal] = mapped_column(Numeric(8, 6), index=True)
    confidence: Mapped[Decimal] = mapped_column(Numeric(8, 6), index=True)
    rank: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    prediction_horizon_hours: Mapped[int] = mapped_column(Integer, default=24)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    components: Mapped[dict[str, float]] = mapped_column(JSON, default=dict)
    reasons: Mapped[list[str]] = mapped_column(JSON, default=list)
    evidence_summary: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )


class TrendEvidencePacket(Base):
    __tablename__ = "trend_evidence_packets"
    __table_args__ = (UniqueConstraint("trend_opportunity_id", "version"),)

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    trend_opportunity_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("trend_opportunities.id"), index=True
    )
    version: Mapped[int] = mapped_column(Integer)
    packet_sha256: Mapped[str] = mapped_column(String(64), index=True)
    thesis: Mapped[str] = mapped_column(Text)
    why_now: Mapped[list[str]] = mapped_column(JSON, default=list)
    sources: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    claims: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    media_refs: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    acquisition_refs: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    packet_metadata: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    generated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
