"""add durable discovery poll attempts and provider quota reservations

Revision ID: 0022_discovery_poll_quota
Revises: 0020_merge_ranked_trend_heads
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0022_discovery_poll_quota"
down_revision: str | None = "0020_merge_ranked_trend_heads"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "trend_watch_source_states",
        sa.Column("source_identity", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "trend_watch_source_states",
        sa.Column("source_quota_limit_per_day", sa.Integer(), nullable=True),
    )
    op.add_column(
        "trend_watch_source_states",
        sa.Column("last_poll_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "trend_watch_source_states",
        sa.Column("rate_limit_reset_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_trend_watch_source_states_source_identity",
        "trend_watch_source_states",
        ["source_identity"],
    )
    op.create_index(
        "ix_trend_watch_source_states_rate_limit_reset_at",
        "trend_watch_source_states",
        ["rate_limit_reset_at"],
    )

    op.create_table(
        "discovery_collection_claims",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("source_identity", sa.String(length=64), nullable=False),
        sa.Column("collection_window_key", sa.String(length=64), nullable=False),
        sa.Column("owner_source_state_id", sa.Uuid(), nullable=False),
        sa.Column("discovery_run_id", sa.Uuid(), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("blocked_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("claim_metadata", sa.JSON(), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
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
        sa.ForeignKeyConstraint(
            ["owner_source_state_id"], ["trend_watch_source_states.id"]
        ),
        sa.ForeignKeyConstraint(["discovery_run_id"], ["discovery_runs.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("source_identity", "collection_window_key"),
    )
    for column in (
        "source_identity",
        "collection_window_key",
        "owner_source_state_id",
        "discovery_run_id",
        "status",
        "lease_expires_at",
        "blocked_until",
        "created_at",
    ):
        op.create_index(
            f"ix_discovery_collection_claims_{column}",
            "discovery_collection_claims",
            [column],
        )

    op.create_table(
        "trend_watch_poll_attempts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("source_state_id", sa.Uuid(), nullable=False),
        sa.Column("collection_claim_id", sa.Uuid(), nullable=True),
        sa.Column("discovery_run_id", sa.Uuid(), nullable=True),
        sa.Column("execution_key", sa.String(length=160), nullable=False),
        sa.Column("source_identity", sa.String(length=64), nullable=False),
        sa.Column("outcome", sa.String(length=32), nullable=False),
        sa.Column("cursor_before", sa.JSON(), nullable=False),
        sa.Column("cursor_after", sa.JSON(), nullable=False),
        sa.Column("candidate_count", sa.Integer(), nullable=False),
        sa.Column("pages", sa.Integer(), nullable=False),
        sa.Column("source_quota_reserved", sa.Integer(), nullable=False),
        sa.Column("source_quota_consumed", sa.Integer(), nullable=False),
        sa.Column("provider_usage", sa.JSON(), nullable=False),
        sa.Column("http_status", sa.Integer(), nullable=True),
        sa.Column("retry_after_seconds", sa.Integer(), nullable=True),
        sa.Column("error_kind", sa.String(length=64), nullable=True),
        sa.Column("attempt_metadata", sa.JSON(), nullable=False),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "candidate_count >= 0",
            name="ck_trend_poll_candidate_count_nonnegative",
        ),
        sa.CheckConstraint("pages >= 0", name="ck_trend_poll_pages_nonnegative"),
        sa.CheckConstraint(
            "source_quota_reserved >= 0 AND source_quota_consumed >= 0",
            name="ck_trend_poll_source_quota_nonnegative",
        ),
        sa.ForeignKeyConstraint(
            ["source_state_id"], ["trend_watch_source_states.id"]
        ),
        sa.ForeignKeyConstraint(
            ["collection_claim_id"], ["discovery_collection_claims.id"]
        ),
        sa.ForeignKeyConstraint(["discovery_run_id"], ["discovery_runs.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("source_state_id", "execution_key"),
    )
    for column in (
        "source_state_id",
        "collection_claim_id",
        "discovery_run_id",
        "execution_key",
        "source_identity",
        "outcome",
        "started_at",
    ):
        op.create_index(
            f"ix_trend_watch_poll_attempts_{column}",
            "trend_watch_poll_attempts",
            [column],
        )

    op.create_table(
        "discovery_provider_quota_windows",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("provider_key", sa.String(length=64), nullable=False),
        sa.Column("bucket_key", sa.String(length=128), nullable=False),
        sa.Column("window_key", sa.String(length=96), nullable=False),
        sa.Column("window_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("window_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("limit_units", sa.Integer(), nullable=False),
        sa.Column("used_units", sa.Integer(), nullable=False),
        sa.Column("reserved_units", sa.Integer(), nullable=False),
        sa.Column("blocked_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("quota_metadata", sa.JSON(), nullable=False),
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
            "limit_units > 0", name="ck_discovery_quota_limit_positive"
        ),
        sa.CheckConstraint(
            "used_units >= 0 AND reserved_units >= 0",
            name="ck_discovery_quota_usage_nonnegative",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("provider_key", "bucket_key", "window_key"),
    )
    for column in (
        "provider_key",
        "bucket_key",
        "window_key",
        "window_start",
        "window_end",
        "blocked_until",
    ):
        op.create_index(
            f"ix_discovery_provider_quota_windows_{column}",
            "discovery_provider_quota_windows",
            [column],
        )

    op.create_table(
        "discovery_quota_reservations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("poll_attempt_id", sa.Uuid(), nullable=False),
        sa.Column("quota_window_id", sa.Uuid(), nullable=False),
        sa.Column("page_key", sa.String(length=64), nullable=False),
        sa.Column("provider_key", sa.String(length=64), nullable=False),
        sa.Column("bucket_key", sa.String(length=128), nullable=False),
        sa.Column("window_key", sa.String(length=96), nullable=False),
        sa.Column("reserved_units", sa.Integer(), nullable=False),
        sa.Column("consumed_units", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("settled_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "reserved_units > 0 AND consumed_units >= 0",
            name="ck_discovery_quota_reservation_units",
        ),
        sa.ForeignKeyConstraint(
            ["poll_attempt_id"], ["trend_watch_poll_attempts.id"]
        ),
        sa.ForeignKeyConstraint(
            ["quota_window_id"], ["discovery_provider_quota_windows.id"]
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("poll_attempt_id", "page_key", "bucket_key"),
    )
    for column in (
        "poll_attempt_id",
        "quota_window_id",
        "page_key",
        "provider_key",
        "bucket_key",
        "window_key",
        "status",
    ):
        op.create_index(
            f"ix_discovery_quota_reservations_{column}",
            "discovery_quota_reservations",
            [column],
        )


def downgrade() -> None:
    op.drop_table("discovery_quota_reservations")
    op.drop_table("discovery_provider_quota_windows")
    op.drop_table("trend_watch_poll_attempts")
    op.drop_table("discovery_collection_claims")
    op.drop_index(
        "ix_trend_watch_source_states_rate_limit_reset_at",
        table_name="trend_watch_source_states",
    )
    op.drop_index(
        "ix_trend_watch_source_states_source_identity",
        table_name="trend_watch_source_states",
    )
    op.drop_column("trend_watch_source_states", "rate_limit_reset_at")
    op.drop_column("trend_watch_source_states", "last_poll_at")
    op.drop_column("trend_watch_source_states", "source_quota_limit_per_day")
    op.drop_column("trend_watch_source_states", "source_identity")
