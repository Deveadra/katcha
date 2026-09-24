"""add controlled opportunity activation policy and run ledger

Revision ID: 0023_opportunity_activation
Revises: 0022_discovery_poll_quota
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0023_opportunity_activation"
down_revision: str | None = "0022_discovery_poll_quota"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "opportunity_activation_policy_versions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("channel_profile_id", sa.Uuid(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("min_opportunity_score", sa.Numeric(8, 6), nullable=False),
        sa.Column("min_confidence", sa.Numeric(8, 6), nullable=False),
        sa.Column("min_calibrated_score", sa.Numeric(8, 6), nullable=True),
        sa.Column("min_rights_readiness", sa.Numeric(8, 6), nullable=False),
        sa.Column("min_lead_minutes", sa.Integer(), nullable=False),
        sa.Column("max_activations_per_day", sa.Integer(), nullable=False),
        sa.Column("cooldown_minutes", sa.Integer(), nullable=False),
        sa.Column("max_planned_backlog", sa.Integer(), nullable=False),
        sa.Column("min_budget_headroom_usd", sa.Numeric(14, 4), nullable=False),
        sa.Column("max_inspected_per_run", sa.Integer(), nullable=False),
        sa.Column("item_count", sa.Integer(), nullable=True),
        sa.Column("policy_metadata", sa.JSON(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "min_opportunity_score >= 0 AND min_opportunity_score <= 1",
            name="ck_activation_policy_opportunity_score",
        ),
        sa.CheckConstraint(
            "min_confidence >= 0 AND min_confidence <= 1",
            name="ck_activation_policy_confidence",
        ),
        sa.CheckConstraint(
            "min_calibrated_score IS NULL OR "
            "(min_calibrated_score >= 0 AND min_calibrated_score <= 1)",
            name="ck_activation_policy_calibrated_score",
        ),
        sa.CheckConstraint(
            "min_rights_readiness >= 0 AND min_rights_readiness <= 1",
            name="ck_activation_policy_rights_readiness",
        ),
        sa.CheckConstraint(
            "min_lead_minutes >= 0",
            name="ck_activation_policy_lead_minutes",
        ),
        sa.CheckConstraint(
            "max_activations_per_day >= 0",
            name="ck_activation_policy_daily_limit",
        ),
        sa.CheckConstraint(
            "cooldown_minutes >= 0",
            name="ck_activation_policy_cooldown",
        ),
        sa.CheckConstraint(
            "max_planned_backlog >= 0",
            name="ck_activation_policy_backlog",
        ),
        sa.CheckConstraint(
            "min_budget_headroom_usd >= 0",
            name="ck_activation_policy_budget_headroom",
        ),
        sa.CheckConstraint(
            "max_inspected_per_run > 0",
            name="ck_activation_policy_inspected_positive",
        ),
        sa.CheckConstraint(
            "item_count IS NULL OR item_count > 0",
            name="ck_activation_policy_item_count_positive",
        ),
        sa.ForeignKeyConstraint(["channel_profile_id"], ["channel_profiles.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("channel_profile_id", "version"),
    )
    for column in ("channel_profile_id", "enabled", "created_at"):
        op.create_index(
            f"ix_opportunity_activation_policy_versions_{column}",
            "opportunity_activation_policy_versions",
            [column],
        )

    op.create_table(
        "opportunity_activation_runs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("channel_profile_id", sa.Uuid(), nullable=False),
        sa.Column("run_key", sa.String(length=160), nullable=False),
        sa.Column("policy_version", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("scanned_count", sa.Integer(), nullable=False),
        sa.Column("activated_count", sa.Integer(), nullable=False),
        sa.Column("skipped_count", sa.Integer(), nullable=False),
        sa.Column("run_metadata", sa.JSON(), nullable=False),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "scanned_count >= 0",
            name="ck_activation_run_scanned_nonnegative",
        ),
        sa.CheckConstraint(
            "activated_count >= 0",
            name="ck_activation_run_activated_nonnegative",
        ),
        sa.CheckConstraint(
            "skipped_count >= 0",
            name="ck_activation_run_skipped_nonnegative",
        ),
        sa.ForeignKeyConstraint(["channel_profile_id"], ["channel_profiles.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("channel_profile_id", "run_key"),
    )
    for column in ("channel_profile_id", "run_key", "status", "started_at"):
        op.create_index(
            f"ix_opportunity_activation_runs_{column}",
            "opportunity_activation_runs",
            [column],
        )

    op.create_table(
        "opportunity_activation_decisions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("activation_run_id", sa.Uuid(), nullable=False),
        sa.Column("channel_profile_id", sa.Uuid(), nullable=False),
        sa.Column("trend_opportunity_id", sa.Uuid(), nullable=False),
        sa.Column("short_episode_id", sa.Uuid(), nullable=True),
        sa.Column("outcome", sa.String(length=32), nullable=False),
        sa.Column("reason", sa.String(length=128), nullable=False),
        sa.Column("activation_key", sa.String(length=64), nullable=True),
        sa.Column("decision_metadata", sa.JSON(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["activation_run_id"], ["opportunity_activation_runs.id"]
        ),
        sa.ForeignKeyConstraint(["channel_profile_id"], ["channel_profiles.id"]),
        sa.ForeignKeyConstraint(["trend_opportunity_id"], ["trend_opportunities.id"]),
        sa.ForeignKeyConstraint(["short_episode_id"], ["short_episodes.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("activation_run_id", "trend_opportunity_id"),
    )
    for column in (
        "activation_run_id",
        "channel_profile_id",
        "trend_opportunity_id",
        "short_episode_id",
        "outcome",
        "reason",
        "created_at",
    ):
        op.create_index(
            f"ix_opportunity_activation_decisions_{column}",
            "opportunity_activation_decisions",
            [column],
        )


def downgrade() -> None:
    op.drop_table("opportunity_activation_decisions")
    op.drop_table("opportunity_activation_runs")
    op.drop_table("opportunity_activation_policy_versions")
