"""youtube publishing and analytics

Revision ID: 0004_youtube_publishing
Revises: 0003_short_production
Create Date: 2026-09-15
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004_youtube_publishing"
down_revision: str | None = "0003_short_production"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "youtube_connections",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("channel_id", sa.String(length=128), nullable=False),
        sa.Column("channel_title", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("scopes", sa.JSON(), nullable=False),
        sa.Column("encrypted_access_token", sa.Text(), nullable=False),
        sa.Column("encrypted_refresh_token", sa.Text(), nullable=False),
        sa.Column("token_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("connection_metadata", sa.JSON(), nullable=False),
        sa.Column("last_refreshed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("channel_id"),
    )
    op.create_index(
        "ix_youtube_connections_channel_id", "youtube_connections", ["channel_id"]
    )
    op.create_index("ix_youtube_connections_status", "youtube_connections", ["status"])

    op.create_table(
        "oauth_states",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("state_hash", sa.String(length=64), nullable=False),
        sa.Column("encrypted_code_verifier", sa.Text(), nullable=False),
        sa.Column("redirect_uri", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("state_hash"),
    )
    op.create_index("ix_oauth_states_state_hash", "oauth_states", ["state_hash"])
    op.create_index("ix_oauth_states_expires_at", "oauth_states", ["expires_at"])

    op.create_table(
        "publications",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("production_id", sa.Uuid(), nullable=False),
        sa.Column("youtube_connection_id", sa.Uuid(), nullable=False),
        sa.Column("workflow_id", sa.String(length=255), nullable=False),
        sa.Column("workflow_attempt", sa.Integer(), nullable=False),
        sa.Column("analytics_workflow_id", sa.String(length=255), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("stage", sa.String(length=64), nullable=False),
        sa.Column("title", sa.String(length=100), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("tags", sa.JSON(), nullable=False),
        sa.Column("category_id", sa.String(length=32), nullable=False),
        sa.Column("privacy_status", sa.String(length=32), nullable=False),
        sa.Column("publish_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("notify_subscribers", sa.Boolean(), nullable=False),
        sa.Column("made_for_kids", sa.Boolean(), nullable=False),
        sa.Column("contains_synthetic_media", sa.Boolean(), nullable=False),
        sa.Column("youtube_video_id", sa.String(length=64), nullable=True),
        sa.Column("encrypted_upload_url", sa.Text(), nullable=True),
        sa.Column("upload_offset", sa.BigInteger(), nullable=False),
        sa.Column("upload_size", sa.BigInteger(), nullable=True),
        sa.Column("processing_status", sa.String(length=64), nullable=True),
        sa.Column("failure_reason", sa.Text(), nullable=True),
        sa.Column("rejection_reason", sa.Text(), nullable=True),
        sa.Column("raw_status", sa.JSON(), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["production_id"], ["productions.id"]),
        sa.ForeignKeyConstraint(["youtube_connection_id"], ["youtube_connections.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("production_id", "youtube_connection_id"),
        sa.UniqueConstraint("workflow_id"),
        sa.UniqueConstraint("analytics_workflow_id"),
    )
    op.create_index("ix_publications_production_id", "publications", ["production_id"])
    op.create_index(
        "ix_publications_youtube_connection_id", "publications", ["youtube_connection_id"]
    )
    op.create_index("ix_publications_workflow_id", "publications", ["workflow_id"])
    op.create_index(
        "ix_publications_analytics_workflow_id", "publications", ["analytics_workflow_id"]
    )
    op.create_index("ix_publications_status", "publications", ["status"])
    op.create_index("ix_publications_youtube_video_id", "publications", ["youtube_video_id"])

    op.create_table(
        "publication_analytics_snapshots",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("publication_id", sa.Uuid(), nullable=False),
        sa.Column("sample_key", sa.String(length=128), nullable=False),
        sa.Column("sampled_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("period_start", sa.Date(), nullable=False),
        sa.Column("period_end", sa.Date(), nullable=False),
        sa.Column("views", sa.BigInteger(), nullable=True),
        sa.Column("engaged_views", sa.BigInteger(), nullable=True),
        sa.Column("estimated_minutes_watched", sa.Numeric(18, 4), nullable=True),
        sa.Column("average_view_duration", sa.Numeric(18, 4), nullable=True),
        sa.Column("average_view_percentage", sa.Numeric(12, 6), nullable=True),
        sa.Column("likes", sa.BigInteger(), nullable=True),
        sa.Column("comments", sa.BigInteger(), nullable=True),
        sa.Column("shares", sa.BigInteger(), nullable=True),
        sa.Column("subscribers_gained", sa.BigInteger(), nullable=True),
        sa.Column("subscribers_lost", sa.BigInteger(), nullable=True),
        sa.Column("estimated_revenue", sa.Numeric(18, 8), nullable=True),
        sa.Column("estimated_ad_revenue", sa.Numeric(18, 8), nullable=True),
        sa.Column("monetized_playbacks", sa.BigInteger(), nullable=True),
        sa.Column("raw_metrics", sa.JSON(), nullable=False),
        sa.Column("raw_monetary", sa.JSON(), nullable=False),
        sa.Column("raw_video", sa.JSON(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["publication_id"], ["publications.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("publication_id", "sample_key"),
    )
    op.create_index(
        "ix_publication_analytics_snapshots_publication_id",
        "publication_analytics_snapshots",
        ["publication_id"],
    )
    op.create_index(
        "ix_publication_analytics_snapshots_sampled_at",
        "publication_analytics_snapshots",
        ["sampled_at"],
    )

    op.create_table(
        "retention_points",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("snapshot_id", sa.Uuid(), nullable=False),
        sa.Column("elapsed_video_time_ratio", sa.Numeric(8, 6), nullable=False),
        sa.Column("audience_watch_ratio", sa.Numeric(12, 8), nullable=True),
        sa.Column("relative_retention_performance", sa.Numeric(12, 8), nullable=True),
        sa.Column("raw_row", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(["snapshot_id"], ["publication_analytics_snapshots.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("snapshot_id", "elapsed_video_time_ratio"),
    )
    op.create_index("ix_retention_points_snapshot_id", "retention_points", ["snapshot_id"])


def downgrade() -> None:
    op.drop_index("ix_retention_points_snapshot_id", table_name="retention_points")
    op.drop_table("retention_points")
    op.drop_index(
        "ix_publication_analytics_snapshots_sampled_at",
        table_name="publication_analytics_snapshots",
    )
    op.drop_index(
        "ix_publication_analytics_snapshots_publication_id",
        table_name="publication_analytics_snapshots",
    )
    op.drop_table("publication_analytics_snapshots")
    op.drop_index("ix_publications_youtube_video_id", table_name="publications")
    op.drop_index("ix_publications_status", table_name="publications")
    op.drop_index("ix_publications_analytics_workflow_id", table_name="publications")
    op.drop_index("ix_publications_workflow_id", table_name="publications")
    op.drop_index("ix_publications_youtube_connection_id", table_name="publications")
    op.drop_index("ix_publications_production_id", table_name="publications")
    op.drop_table("publications")
    op.drop_index("ix_oauth_states_expires_at", table_name="oauth_states")
    op.drop_index("ix_oauth_states_state_hash", table_name="oauth_states")
    op.drop_table("oauth_states")
    op.drop_index("ix_youtube_connections_status", table_name="youtube_connections")
    op.drop_index("ix_youtube_connections_channel_id", table_name="youtube_connections")
    op.drop_table("youtube_connections")
