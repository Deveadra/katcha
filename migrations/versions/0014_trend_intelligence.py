"""add cross-source trend intelligence

Revision ID: 0014_trend_intelligence
Revises: 0013_discovery_observations
Create Date: 2026-09-16
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0014_trend_intelligence"
down_revision: str | None = "0013_discovery_observations"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "channel_trend_watch_versions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("channel_profile_id", sa.Uuid(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("interests", sa.JSON(), nullable=False),
        sa.Column("excluded_terms", sa.JSON(), nullable=False),
        sa.Column("entities", sa.JSON(), nullable=False),
        sa.Column("platforms", sa.JSON(), nullable=False),
        sa.Column("languages", sa.JSON(), nullable=False),
        sa.Column("regions", sa.JSON(), nullable=False),
        sa.Column("source_weights", sa.JSON(), nullable=False),
        sa.Column("freshness_horizon_hours", sa.Integer(), nullable=False),
        sa.Column("min_confidence", sa.Numeric(8, 6), nullable=False),
        sa.Column("opportunity_threshold", sa.Numeric(8, 6), nullable=False),
        sa.Column("watch_metadata", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.CheckConstraint(
            "freshness_horizon_hours > 0",
            name="ck_trend_watch_freshness_positive",
        ),
        sa.CheckConstraint(
            "min_confidence >= 0 AND min_confidence <= 1",
            name="ck_trend_watch_min_confidence",
        ),
        sa.CheckConstraint(
            "opportunity_threshold >= 0 AND opportunity_threshold <= 1",
            name="ck_trend_watch_opportunity_threshold",
        ),
        sa.ForeignKeyConstraint(["channel_profile_id"], ["channel_profiles.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("channel_profile_id", "version"),
    )
    op.create_index(
        "ix_channel_trend_watch_versions_channel_profile_id",
        "channel_trend_watch_versions",
        ["channel_profile_id"],
    )
    op.create_index(
        "ix_channel_trend_watch_versions_created_at",
        "channel_trend_watch_versions",
        ["created_at"],
    )

    op.create_table(
        "trend_topics",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("topic_key", sa.String(255), nullable=False),
        sa.Column("display_name", sa.Text(), nullable=False),
        sa.Column("aliases", sa.JSON(), nullable=False),
        sa.Column("tags", sa.JSON(), nullable=False),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("topic_metadata", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("topic_key"),
    )
    op.create_index("ix_trend_topics_topic_key", "trend_topics", ["topic_key"])
    op.create_index("ix_trend_topics_first_seen_at", "trend_topics", ["first_seen_at"])
    op.create_index("ix_trend_topics_last_seen_at", "trend_topics", ["last_seen_at"])

    op.create_table(
        "trend_signals",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("provider_key", sa.String(64), nullable=False),
        sa.Column("external_id", sa.String(512), nullable=False),
        sa.Column("observation_key", sa.String(128), nullable=False),
        sa.Column("canonical_url", sa.Text(), nullable=True),
        sa.Column("source_kind", sa.String(64), nullable=False),
        sa.Column("source_name", sa.String(255), nullable=True),
        sa.Column("independence_key", sa.String(512), nullable=False),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column("body_excerpt", sa.Text(), nullable=True),
        sa.Column("author", sa.String(255), nullable=True),
        sa.Column("community", sa.String(255), nullable=True),
        sa.Column("language", sa.String(32), nullable=True),
        sa.Column("region", sa.String(64), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("metrics", sa.JSON(), nullable=False),
        sa.Column("media_refs", sa.JSON(), nullable=False),
        sa.Column("content_fingerprint", sa.String(128), nullable=True),
        sa.Column("signal_metadata", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("provider_key", "external_id", "observation_key"),
    )
    for column in (
        "provider_key",
        "external_id",
        "source_kind",
        "independence_key",
        "language",
        "region",
        "published_at",
        "observed_at",
        "content_fingerprint",
    ):
        op.create_index(f"ix_trend_signals_{column}", "trend_signals", [column])

    op.create_table(
        "trend_topic_signals",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("trend_topic_id", sa.Uuid(), nullable=False),
        sa.Column("trend_signal_id", sa.Uuid(), nullable=False),
        sa.Column("match_confidence", sa.Numeric(8, 6), nullable=False),
        sa.Column("match_reasons", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.CheckConstraint(
            "match_confidence >= 0 AND match_confidence <= 1",
            name="ck_trend_topic_signal_confidence",
        ),
        sa.ForeignKeyConstraint(["trend_signal_id"], ["trend_signals.id"]),
        sa.ForeignKeyConstraint(["trend_topic_id"], ["trend_topics.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("trend_topic_id", "trend_signal_id"),
    )
    op.create_index(
        "ix_trend_topic_signals_trend_topic_id",
        "trend_topic_signals",
        ["trend_topic_id"],
    )
    op.create_index(
        "ix_trend_topic_signals_trend_signal_id",
        "trend_topic_signals",
        ["trend_signal_id"],
    )

    op.create_table(
        "trend_opportunities",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("channel_profile_id", sa.Uuid(), nullable=False),
        sa.Column("trend_topic_id", sa.Uuid(), nullable=False),
        sa.Column("watch_version", sa.Integer(), nullable=False),
        sa.Column("run_key", sa.String(160), nullable=False),
        sa.Column("lifecycle", sa.String(32), nullable=False),
        sa.Column("opportunity_score", sa.Numeric(8, 6), nullable=False),
        sa.Column("confidence", sa.Numeric(8, 6), nullable=False),
        sa.Column("rank", sa.Integer(), nullable=True),
        sa.Column("prediction_horizon_hours", sa.Integer(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("components", sa.JSON(), nullable=False),
        sa.Column("reasons", sa.JSON(), nullable=False),
        sa.Column("evidence_summary", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.CheckConstraint(
            "opportunity_score >= 0 AND opportunity_score <= 1",
            name="ck_trend_opportunity_score",
        ),
        sa.CheckConstraint(
            "confidence >= 0 AND confidence <= 1",
            name="ck_trend_opportunity_confidence",
        ),
        sa.CheckConstraint(
            "prediction_horizon_hours > 0",
            name="ck_trend_prediction_horizon_positive",
        ),
        sa.ForeignKeyConstraint(["channel_profile_id"], ["channel_profiles.id"]),
        sa.ForeignKeyConstraint(["trend_topic_id"], ["trend_topics.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("channel_profile_id", "trend_topic_id", "run_key"),
    )
    for column in (
        "channel_profile_id",
        "trend_topic_id",
        "run_key",
        "lifecycle",
        "opportunity_score",
        "confidence",
        "rank",
        "expires_at",
        "created_at",
    ):
        op.create_index(
            f"ix_trend_opportunities_{column}",
            "trend_opportunities",
            [column],
        )

    op.create_table(
        "trend_evidence_packets",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("trend_opportunity_id", sa.Uuid(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("packet_sha256", sa.String(64), nullable=False),
        sa.Column("thesis", sa.Text(), nullable=False),
        sa.Column("why_now", sa.JSON(), nullable=False),
        sa.Column("sources", sa.JSON(), nullable=False),
        sa.Column("claims", sa.JSON(), nullable=False),
        sa.Column("media_refs", sa.JSON(), nullable=False),
        sa.Column("acquisition_refs", sa.JSON(), nullable=False),
        sa.Column("packet_metadata", sa.JSON(), nullable=False),
        sa.Column("generated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["trend_opportunity_id"], ["trend_opportunities.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("trend_opportunity_id", "version"),
    )
    op.create_index(
        "ix_trend_evidence_packets_trend_opportunity_id",
        "trend_evidence_packets",
        ["trend_opportunity_id"],
    )
    op.create_index(
        "ix_trend_evidence_packets_packet_sha256",
        "trend_evidence_packets",
        ["packet_sha256"],
    )
    op.create_index(
        "ix_trend_evidence_packets_generated_at",
        "trend_evidence_packets",
        ["generated_at"],
    )


def downgrade() -> None:
    op.drop_table("trend_evidence_packets")
    op.drop_table("trend_opportunities")
    op.drop_table("trend_topic_signals")
    op.drop_table("trend_signals")
    op.drop_table("trend_topics")
    op.drop_table("channel_trend_watch_versions")
