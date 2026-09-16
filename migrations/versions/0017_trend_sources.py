"""add durable trend source polling

Revision ID: 0017_trend_sources
Revises: 0016_short_episode_editorial
Create Date: 2026-09-16
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0017_trend_sources"
down_revision: str | None = "0016_short_episode_editorial"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "trend_source_subscriptions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("channel_profile_id", sa.Uuid(), nullable=False),
        sa.Column("subscription_key", sa.String(length=128), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("adapter_key", sa.String(length=64), nullable=False),
        sa.Column("adapter_version", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("source_query", sa.JSON(), nullable=False),
        sa.Column("cursor", sa.JSON(), nullable=False),
        sa.Column("poll_interval_seconds", sa.Integer(), nullable=False),
        sa.Column("health_status", sa.String(length=32), nullable=False),
        sa.Column("consecutive_failures", sa.Integer(), nullable=False),
        sa.Column("last_success_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_failure_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_poll_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("backoff_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error_kind", sa.String(length=64), nullable=True),
        sa.Column("last_error_summary", sa.Text(), nullable=True),
        sa.Column("source_metadata", sa.JSON(), nullable=False),
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
            "poll_interval_seconds >= 60",
            name="ck_trend_source_poll_interval_minimum",
        ),
        sa.CheckConstraint(
            "consecutive_failures >= 0",
            name="ck_trend_source_failures_nonnegative",
        ),
        sa.ForeignKeyConstraint(["channel_profile_id"], ["channel_profiles.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("channel_profile_id", "subscription_key"),
    )
    for column in (
        "channel_profile_id",
        "subscription_key",
        "adapter_key",
        "status",
        "health_status",
        "next_poll_at",
        "backoff_until",
        "created_at",
    ):
        op.create_index(
            f"ix_trend_source_subscriptions_{column}",
            "trend_source_subscriptions",
            [column],
        )

    op.create_table(
        "trend_source_polls",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("trend_source_subscription_id", sa.Uuid(), nullable=False),
        sa.Column("run_key", sa.String(length=160), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("item_count", sa.Integer(), nullable=False),
        sa.Column("cursor_before", sa.JSON(), nullable=False),
        sa.Column("cursor_after", sa.JSON(), nullable=False),
        sa.Column("error_kind", sa.String(length=64), nullable=True),
        sa.Column("retry_after_seconds", sa.Integer(), nullable=True),
        sa.Column("poll_metadata", sa.JSON(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "item_count >= 0",
            name="ck_trend_source_poll_item_count",
        ),
        sa.ForeignKeyConstraint(
            ["trend_source_subscription_id"],
            ["trend_source_subscriptions.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("trend_source_subscription_id", "run_key"),
    )
    for column in (
        "trend_source_subscription_id",
        "run_key",
        "status",
        "created_at",
    ):
        op.create_index(
            f"ix_trend_source_polls_{column}",
            "trend_source_polls",
            [column],
        )


def downgrade() -> None:
    op.drop_table("trend_source_polls")
    op.drop_table("trend_source_subscriptions")
