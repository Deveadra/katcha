"""harden channel budget accounting

Revision ID: 0010_budget_reservations
Revises: 0009_ranking_run_key
Create Date: 2026-09-15
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0010_budget_reservations"
down_revision: str | None = "0009_ranking_run_key"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "channel_strategy_versions",
        sa.Column(
            "monthly_base_budget_usd",
            sa.Numeric(14, 4),
            nullable=False,
            server_default="0",
        ),
    )
    op.execute(
        "UPDATE channel_strategy_versions "
        "SET monthly_base_budget_usd = monthly_hard_budget_usd"
    )
    op.alter_column(
        "channel_strategy_versions",
        "monthly_base_budget_usd",
        server_default=None,
    )
    op.create_check_constraint(
        "ck_strategy_base_budget_nonnegative",
        "channel_strategy_versions",
        "monthly_base_budget_usd >= 0",
    )
    op.create_check_constraint(
        "ck_strategy_base_within_hard_budget",
        "channel_strategy_versions",
        "monthly_base_budget_usd <= monthly_hard_budget_usd",
    )

    for name in (
        "base_budget_usd",
        "effective_budget_usd",
        "reserved_ai_cost_usd",
        "burn_rate_usd_per_day",
        "projected_month_end_spend_usd",
    ):
        op.add_column(
            "channel_economics_snapshots",
            sa.Column(
                name,
                sa.Numeric(18, 8),
                nullable=False,
                server_default="0",
            ),
        )
        op.alter_column(
            "channel_economics_snapshots",
            name,
            server_default=None,
        )

    op.create_table(
        "ai_budget_reservations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("channel_profile_id", sa.Uuid(), nullable=False),
        sa.Column("reservation_key", sa.String(length=255), nullable=False),
        sa.Column("task", sa.String(length=64), nullable=False),
        sa.Column("reference_type", sa.String(length=64), nullable=True),
        sa.Column("reference_id", sa.String(length=128), nullable=True),
        sa.Column("estimated_cost_usd", sa.Numeric(18, 8), nullable=False),
        sa.Column("actual_cost_usd", sa.Numeric(18, 8), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("settled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reservation_metadata", sa.JSON(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "estimated_cost_usd >= 0",
            name="ck_budget_reservation_estimated_nonnegative",
        ),
        sa.CheckConstraint(
            "actual_cost_usd IS NULL OR actual_cost_usd >= 0",
            name="ck_budget_reservation_actual_nonnegative",
        ),
        sa.ForeignKeyConstraint(
            ["channel_profile_id"],
            ["channel_profiles.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "channel_profile_id",
            "reservation_key",
            name="uq_ai_budget_reservations_channel_key",
        ),
    )
    op.create_index(
        "ix_ai_budget_reservations_channel_profile_id",
        "ai_budget_reservations",
        ["channel_profile_id"],
    )
    op.create_index(
        "ix_ai_budget_reservations_task",
        "ai_budget_reservations",
        ["task"],
    )
    op.create_index(
        "ix_ai_budget_reservations_status",
        "ai_budget_reservations",
        ["status"],
    )
    op.create_index(
        "ix_ai_budget_reservations_expires_at",
        "ai_budget_reservations",
        ["expires_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_ai_budget_reservations_expires_at",
        table_name="ai_budget_reservations",
    )
    op.drop_index(
        "ix_ai_budget_reservations_status",
        table_name="ai_budget_reservations",
    )
    op.drop_index(
        "ix_ai_budget_reservations_task",
        table_name="ai_budget_reservations",
    )
    op.drop_index(
        "ix_ai_budget_reservations_channel_profile_id",
        table_name="ai_budget_reservations",
    )
    op.drop_table("ai_budget_reservations")

    for name in reversed(
        (
            "base_budget_usd",
            "effective_budget_usd",
            "reserved_ai_cost_usd",
            "burn_rate_usd_per_day",
            "projected_month_end_spend_usd",
        )
    ):
        op.drop_column("channel_economics_snapshots", name)

    op.drop_constraint(
        "ck_strategy_base_within_hard_budget",
        "channel_strategy_versions",
        type_="check",
    )
    op.drop_constraint(
        "ck_strategy_base_budget_nonnegative",
        "channel_strategy_versions",
        type_="check",
    )
    op.drop_column("channel_strategy_versions", "monthly_base_budget_usd")
