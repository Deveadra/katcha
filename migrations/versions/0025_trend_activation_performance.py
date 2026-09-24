"""add trend activation performance optimization snapshots

Revision ID: 0025_trend_activation_performance
Revises: 0024_channel_edit_blueprints
Create Date: 2026-09-24
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0025_trend_activation_performance"
down_revision: str | None = "0024_channel_edit_blueprints"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "trend_activation_performance_snapshots",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("channel_profile_id", sa.Uuid(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("run_key", sa.String(length=160), nullable=False),
        sa.Column("policy_version", sa.Integer(), nullable=True),
        sa.Column("calibration_version", sa.Integer(), nullable=True),
        sa.Column("economics_snapshot_id", sa.Uuid(), nullable=True),
        sa.Column("decision_count", sa.Integer(), nullable=False),
        sa.Column("planned_count", sa.Integer(), nullable=False),
        sa.Column("published_count", sa.Integer(), nullable=False),
        sa.Column("outcome_count", sa.Integer(), nullable=False),
        sa.Column("opportunity_to_plan_rate", sa.Numeric(8, 6), nullable=False),
        sa.Column("plan_to_publish_rate", sa.Numeric(8, 6), nullable=False),
        sa.Column(
            "median_opportunity_to_plan_minutes",
            sa.Numeric(14, 4),
            nullable=True,
        ),
        sa.Column(
            "median_plan_to_publish_minutes",
            sa.Numeric(14, 4),
            nullable=True,
        ),
        sa.Column(
            "median_opportunity_to_publish_minutes",
            sa.Numeric(14, 4),
            nullable=True,
        ),
        sa.Column("revenue_usd", sa.Numeric(18, 8), nullable=False),
        sa.Column("attributed_cost_usd", sa.Numeric(18, 8), nullable=False),
        sa.Column("contribution_margin_usd", sa.Numeric(18, 8), nullable=False),
        sa.Column("latest_views", sa.Integer(), nullable=False),
        sa.Column("mean_lift_ratio", sa.Numeric(14, 6), nullable=True),
        sa.Column("realized_breakout_rate", sa.Numeric(8, 6), nullable=True),
        sa.Column("missed_reason_counts", sa.JSON(), nullable=False),
        sa.Column("funnel_metrics", sa.JSON(), nullable=False),
        sa.Column("recommendation_status", sa.String(length=48), nullable=False),
        sa.Column("recommendation", sa.JSON(), nullable=False),
        sa.Column("sample_window_start", sa.DateTime(timezone=True), nullable=True),
        sa.Column("sample_window_end", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "decision_count >= 0",
            name="ck_trend_activation_performance_decisions",
        ),
        sa.CheckConstraint(
            "planned_count >= 0",
            name="ck_trend_activation_performance_planned",
        ),
        sa.CheckConstraint(
            "published_count >= 0",
            name="ck_trend_activation_performance_published",
        ),
        sa.CheckConstraint(
            "outcome_count >= 0",
            name="ck_trend_activation_performance_outcomes",
        ),
        sa.CheckConstraint(
            "opportunity_to_plan_rate >= 0 AND opportunity_to_plan_rate <= 1",
            name="ck_trend_activation_performance_plan_rate",
        ),
        sa.CheckConstraint(
            "plan_to_publish_rate >= 0 AND plan_to_publish_rate <= 1",
            name="ck_trend_activation_performance_publish_rate",
        ),
        sa.ForeignKeyConstraint(["channel_profile_id"], ["channel_profiles.id"]),
        sa.ForeignKeyConstraint(
            ["economics_snapshot_id"],
            ["channel_economics_snapshots.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("channel_profile_id", "version"),
        sa.UniqueConstraint("channel_profile_id", "run_key"),
    )
    for column in (
        "channel_profile_id",
        "run_key",
        "economics_snapshot_id",
        "recommendation_status",
        "created_at",
    ):
        op.create_index(
            f"ix_trend_activation_performance_snapshots_{column}",
            "trend_activation_performance_snapshots",
            [column],
        )


def downgrade() -> None:
    op.drop_table("trend_activation_performance_snapshots")
