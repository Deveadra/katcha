"""add channel-scoped trend discovery reliability

Revision ID: 0020_trend_discovery_reliability
Revises: 0019_ranked_episode_render_publishing
Create Date: 2026-09-16
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0020_trend_discovery_reliability"
down_revision: str | None = "0019_ranked_episode_render_publishing"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "topic_watch_versions",
        sa.Column("channel_profile_id", sa.Uuid(), nullable=True),
    )
    op.add_column(
        "topic_watch_versions",
        sa.Column(
            "scope_key",
            sa.String(length=64),
            nullable=False,
            server_default="global",
        ),
    )
    op.create_foreign_key(
        "fk_topic_watch_channel_profile",
        "topic_watch_versions",
        "channel_profiles",
        ["channel_profile_id"],
        ["id"],
    )
    op.create_index(
        "ix_topic_watch_versions_channel_profile_id",
        "topic_watch_versions",
        ["channel_profile_id"],
    )
    op.create_index(
        "ix_topic_watch_versions_scope_key",
        "topic_watch_versions",
        ["scope_key"],
    )
    op.drop_constraint(
        "topic_watch_versions_watch_key_version_key",
        "topic_watch_versions",
        type_="unique",
    )
    op.create_unique_constraint(
        "uq_topic_watch_scope_key_version",
        "topic_watch_versions",
        ["scope_key", "watch_key", "version"],
    )

    op.create_table(
        "trend_watch_source_states",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("topic_watch_id", sa.Uuid(), nullable=False),
        sa.Column("adapter_index", sa.Integer(), nullable=False),
        sa.Column("adapter_key", sa.String(length=64), nullable=False),
        sa.Column("adapter_version", sa.String(length=64), nullable=False),
        sa.Column("health_status", sa.String(length=32), nullable=False),
        sa.Column("consecutive_failures", sa.Integer(), nullable=False),
        sa.Column("cursor", sa.JSON(), nullable=False),
        sa.Column("last_discovery_run_id", sa.Uuid(), nullable=True),
        sa.Column("last_success_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_failure_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("backoff_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error_kind", sa.String(length=64), nullable=True),
        sa.Column("last_error_summary", sa.Text(), nullable=True),
        sa.Column("state_metadata", sa.JSON(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "adapter_index >= 0",
            name="ck_trend_watch_source_adapter_index_nonnegative",
        ),
        sa.CheckConstraint(
            "consecutive_failures >= 0",
            name="ck_trend_watch_source_failures_nonnegative",
        ),
        sa.ForeignKeyConstraint(
            ["topic_watch_id"],
            ["topic_watch_versions.id"],
        ),
        sa.ForeignKeyConstraint(
            ["last_discovery_run_id"],
            ["discovery_runs.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("topic_watch_id", "adapter_index"),
    )
    for column in (
        "topic_watch_id",
        "adapter_key",
        "health_status",
        "last_discovery_run_id",
        "backoff_until",
        "created_at",
    ):
        op.create_index(
            f"ix_trend_watch_source_states_{column}",
            "trend_watch_source_states",
            [column],
        )


def downgrade() -> None:
    op.drop_table("trend_watch_source_states")
    op.drop_constraint(
        "uq_topic_watch_scope_key_version",
        "topic_watch_versions",
        type_="unique",
    )
    op.create_unique_constraint(
        "topic_watch_versions_watch_key_version_key",
        "topic_watch_versions",
        ["watch_key", "version"],
    )
    op.drop_index(
        "ix_topic_watch_versions_scope_key",
        table_name="topic_watch_versions",
    )
    op.drop_index(
        "ix_topic_watch_versions_channel_profile_id",
        table_name="topic_watch_versions",
    )
    op.drop_constraint(
        "fk_topic_watch_channel_profile",
        "topic_watch_versions",
        type_="foreignkey",
    )
    op.drop_column("topic_watch_versions", "scope_key")
    op.drop_column("topic_watch_versions", "channel_profile_id")
