"""add discovery source health and poll history

Revision ID: 0020_discovery_source_health
Revises: 0019_ranked_episode_render_publishing
Create Date: 2026-09-17
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0020_discovery_source_health"
down_revision: str | None = "0019_ranked_episode_render_publishing"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "discovery_source_states",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("source_key", sa.String(length=64), nullable=False),
        sa.Column("adapter_key", sa.String(length=64), nullable=False),
        sa.Column("adapter_version", sa.String(length=64), nullable=False),
        sa.Column("query_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("effective_query", sa.JSON(), nullable=False),
        sa.Column("health_status", sa.String(length=32), nullable=False),
        sa.Column("consecutive_failures", sa.Integer(), nullable=False),
        sa.Column("total_polls", sa.Integer(), nullable=False),
        sa.Column("total_successes", sa.Integer(), nullable=False),
        sa.Column("total_empty_successes", sa.Integer(), nullable=False),
        sa.Column("total_failures", sa.Integer(), nullable=False),
        sa.Column("total_rate_limits", sa.Integer(), nullable=False),
        sa.Column("cursor", sa.JSON(), nullable=False),
        sa.Column("last_outcome", sa.String(length=32), nullable=True),
        sa.Column("last_error_kind", sa.String(length=64), nullable=True),
        sa.Column("last_poll_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_success_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_failure_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_eligible_poll_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("rate_limit_reset_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("quota_limit_per_day", sa.Integer(), nullable=True),
        sa.Column("quota_used", sa.Integer(), nullable=False),
        sa.Column("quota_window_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("state_metadata", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "consecutive_failures >= 0", name="ck_source_failures_nonnegative"
        ),
        sa.CheckConstraint("total_polls >= 0", name="ck_source_total_polls_nonnegative"),
        sa.CheckConstraint("quota_used >= 0", name="ck_source_quota_used_nonnegative"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("source_key"),
    )
    op.create_index("ix_discovery_source_states_source_key", "discovery_source_states", ["source_key"], unique=True)
    op.create_index("ix_discovery_source_states_adapter_key", "discovery_source_states", ["adapter_key"], unique=False)
    op.create_index("ix_discovery_source_states_query_fingerprint", "discovery_source_states", ["query_fingerprint"], unique=False)
    op.create_index("ix_discovery_source_states_health_status", "discovery_source_states", ["health_status"], unique=False)
    op.create_index("ix_discovery_source_states_last_outcome", "discovery_source_states", ["last_outcome"], unique=False)
    op.create_index("ix_discovery_source_states_last_poll_at", "discovery_source_states", ["last_poll_at"], unique=False)
    op.create_index("ix_discovery_source_states_next_eligible_poll_at", "discovery_source_states", ["next_eligible_poll_at"], unique=False)

    op.create_table(
        "discovery_poll_attempts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("source_state_id", sa.Uuid(), nullable=False),
        sa.Column("discovery_run_id", sa.Uuid(), nullable=True),
        sa.Column("attempt_key", sa.String(length=160), nullable=False),
        sa.Column("outcome", sa.String(length=32), nullable=False),
        sa.Column("candidate_count", sa.Integer(), nullable=False),
        sa.Column("pages", sa.Integer(), nullable=False),
        sa.Column("cursor_before", sa.JSON(), nullable=False),
        sa.Column("cursor_after", sa.JSON(), nullable=False),
        sa.Column("http_status", sa.Integer(), nullable=True),
        sa.Column("retry_after_seconds", sa.Integer(), nullable=True),
        sa.Column("error_kind", sa.String(length=64), nullable=True),
        sa.Column("attempt_metadata", sa.JSON(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("candidate_count >= 0", name="ck_poll_candidate_count_nonnegative"),
        sa.CheckConstraint("pages >= 0", name="ck_poll_pages_nonnegative"),
        sa.ForeignKeyConstraint(["discovery_run_id"], ["discovery_runs.id"]),
        sa.ForeignKeyConstraint(["source_state_id"], ["discovery_source_states.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("source_state_id", "attempt_key"),
    )
    op.create_index("ix_discovery_poll_attempts_source_state_id", "discovery_poll_attempts", ["source_state_id"], unique=False)
    op.create_index("ix_discovery_poll_attempts_discovery_run_id", "discovery_poll_attempts", ["discovery_run_id"], unique=False)
    op.create_index("ix_discovery_poll_attempts_attempt_key", "discovery_poll_attempts", ["attempt_key"], unique=False)
    op.create_index("ix_discovery_poll_attempts_outcome", "discovery_poll_attempts", ["outcome"], unique=False)
    op.create_index("ix_discovery_poll_attempts_started_at", "discovery_poll_attempts", ["started_at"], unique=False)
    op.create_index("ix_discovery_poll_attempts_completed_at", "discovery_poll_attempts", ["completed_at"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_discovery_poll_attempts_completed_at", table_name="discovery_poll_attempts")
    op.drop_index("ix_discovery_poll_attempts_started_at", table_name="discovery_poll_attempts")
    op.drop_index("ix_discovery_poll_attempts_outcome", table_name="discovery_poll_attempts")
    op.drop_index("ix_discovery_poll_attempts_attempt_key", table_name="discovery_poll_attempts")
    op.drop_index("ix_discovery_poll_attempts_discovery_run_id", table_name="discovery_poll_attempts")
    op.drop_index("ix_discovery_poll_attempts_source_state_id", table_name="discovery_poll_attempts")
    op.drop_table("discovery_poll_attempts")

    op.drop_index("ix_discovery_source_states_next_eligible_poll_at", table_name="discovery_source_states")
    op.drop_index("ix_discovery_source_states_last_poll_at", table_name="discovery_source_states")
    op.drop_index("ix_discovery_source_states_last_outcome", table_name="discovery_source_states")
    op.drop_index("ix_discovery_source_states_health_status", table_name="discovery_source_states")
    op.drop_index("ix_discovery_source_states_query_fingerprint", table_name="discovery_source_states")
    op.drop_index("ix_discovery_source_states_adapter_key", table_name="discovery_source_states")
    op.drop_index("ix_discovery_source_states_source_key", table_name="discovery_source_states")
    op.drop_table("discovery_source_states")
