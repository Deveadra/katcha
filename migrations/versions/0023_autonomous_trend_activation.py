"""add autonomous trend activation policies and decision ledger

Revision ID: 0023_autonomous_trend_activation
Revises: 0022_discovery_poll_quota
Create Date: 2026-09-24
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0023_autonomous_trend_activation"
down_revision: str | None = "0022_discovery_poll_quota"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "trend_activation_policy_versions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("channel_profile_id", sa.Uuid(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("min_opportunity_score", sa.Numeric(8, 6), nullable=False),
        sa.Column("min_confidence", sa.Numeric(8, 6), nullable=False),
        sa.Column("min_calibrated_score", sa.Numeric(8, 6), nullable=True),
        sa.Column("min_rights_readiness", sa.Numeric(8, 6), nullable=False),
        sa.Column("min_lead_time_minutes", sa.Integer(), nullable=False),
        sa.Column("max_activations_per_day", sa.Integer(), nullable=False),
        sa.Column("cooldown_minutes", sa.Integer(), nullable=False),
        sa.Column("max_backlog", sa.Integer(), nullable=False),
        sa.Column("min_budget_headroom_usd", sa.Numeric(14, 4), nullable=False),
        sa.Column("max_opportunities_per_run", sa.Integer(), nullable=False),
        sa.Column("policy_metadata", sa.JSON(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "min_opportunity_score >= 0 AND min_opportunity_score <= 1",
            name="ck_trend_activation_policy_min_score",
        ),
        sa.CheckConstraint(
            "min_confidence >= 0 AND min_confidence <= 1",
            name="ck_trend_activation_policy_min_confidence",
        ),
        sa.CheckConstraint(
            "min_calibrated_score IS NULL OR "
            "(min_calibrated_score >= 0 AND min_calibrated_score <= 1)",
            name="ck_trend_activation_policy_min_calibrated",
        ),
        sa.CheckConstraint(
            "min_rights_readiness >= 0 AND min_rights_readiness <= 1",
            name="ck_trend_activation_policy_rights_readiness",
        ),
        sa.CheckConstraint(
            "min_lead_time_minutes >= 0",
            name="ck_trend_activation_policy_lead_time",
        ),
        sa.CheckConstraint(
            "max_activations_per_day >= 0",
            name="ck_trend_activation_policy_daily_cap",
        ),
        sa.CheckConstraint(
            "cooldown_minutes >= 0",
            name="ck_trend_activation_policy_cooldown",
        ),
        sa.CheckConstraint(
            "max_backlog >= 0",
            name="ck_trend_activation_policy_backlog",
        ),
        sa.CheckConstraint(
            "min_budget_headroom_usd >= 0",
            name="ck_trend_activation_policy_budget_headroom",
        ),
        sa.CheckConstraint(
            "max_opportunities_per_run > 0",
            name="ck_trend_activation_policy_max_inspected",
        ),
        sa.ForeignKeyConstraint(["channel_profile_id"], ["channel_profiles.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("channel_profile_id", "version"),
    )
    for column in ("channel_profile_id", "enabled", "created_at"):
        op.create_index(
            f"ix_trend_activation_policy_versions_{column}",
            "trend_activation_policy_versions",
            [column],
        )

    op.create_table(
        "trend_activation_runs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("channel_profile_id", sa.Uuid(), nullable=False),
        sa.Column("policy_version", sa.Integer(), nullable=True),
        sa.Column("run_key", sa.String(length=160), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("inspected_count", sa.Integer(), nullable=False),
        sa.Column("activated_count", sa.Integer(), nullable=False),
        sa.Column("deferred_count", sa.Integer(), nullable=False),
        sa.Column("skipped_count", sa.Integer(), nullable=False),
        sa.Column("run_metadata", sa.JSON(), nullable=False),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "inspected_count >= 0", name="ck_trend_activation_run_inspected"
        ),
        sa.CheckConstraint(
            "activated_count >= 0", name="ck_trend_activation_run_activated"
        ),
        sa.CheckConstraint(
            "deferred_count >= 0", name="ck_trend_activation_run_deferred"
        ),
        sa.CheckConstraint(
            "skipped_count >= 0", name="ck_trend_activation_run_skipped"
        ),
        sa.ForeignKeyConstraint(["channel_profile_id"], ["channel_profiles.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("channel_profile_id", "run_key"),
    )
    for column in ("channel_profile_id", "run_key", "status", "started_at"):
        op.create_index(
            f"ix_trend_activation_runs_{column}",
            "trend_activation_runs",
            [column],
        )

    op.create_table(
        "trend_activation_decisions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("activation_run_id", sa.Uuid(), nullable=False),
        sa.Column("channel_profile_id", sa.Uuid(), nullable=False),
        sa.Column("trend_opportunity_id", sa.Uuid(), nullable=False),
        sa.Column("trend_evidence_packet_id", sa.Uuid(), nullable=True),
        sa.Column("short_episode_id", sa.Uuid(), nullable=True),
        sa.Column("activation_key", sa.String(length=128), nullable=True),
        sa.Column("decision", sa.String(length=48), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("opportunity_score", sa.Numeric(8, 6), nullable=False),
        sa.Column("confidence", sa.Numeric(8, 6), nullable=False),
        sa.Column("calibrated_score", sa.Numeric(8, 6), nullable=True),
        sa.Column("rights_readiness", sa.Numeric(8, 6), nullable=False),
        sa.Column("remaining_lead_minutes", sa.Integer(), nullable=False),
        sa.Column("decision_snapshot", sa.JSON(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "opportunity_score >= 0 AND opportunity_score <= 1",
            name="ck_trend_activation_decision_score",
        ),
        sa.CheckConstraint(
            "confidence >= 0 AND confidence <= 1",
            name="ck_trend_activation_decision_confidence",
        ),
        sa.CheckConstraint(
            "calibrated_score IS NULL OR "
            "(calibrated_score >= 0 AND calibrated_score <= 1)",
            name="ck_trend_activation_decision_calibrated",
        ),
        sa.CheckConstraint(
            "rights_readiness >= 0 AND rights_readiness <= 1",
            name="ck_trend_activation_decision_rights_readiness",
        ),
        sa.ForeignKeyConstraint(["activation_run_id"], ["trend_activation_runs.id"]),
        sa.ForeignKeyConstraint(["channel_profile_id"], ["channel_profiles.id"]),
        sa.ForeignKeyConstraint(["trend_opportunity_id"], ["trend_opportunities.id"]),
        sa.ForeignKeyConstraint(
            ["trend_evidence_packet_id"], ["trend_evidence_packets.id"]
        ),
        sa.ForeignKeyConstraint(["short_episode_id"], ["short_episodes.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("activation_run_id", "trend_opportunity_id"),
    )
    for column in (
        "activation_run_id",
        "channel_profile_id",
        "trend_opportunity_id",
        "trend_evidence_packet_id",
        "short_episode_id",
        "activation_key",
        "decision",
        "created_at",
    ):
        op.create_index(
            f"ix_trend_activation_decisions_{column}",
            "trend_activation_decisions",
            [column],
        )


def downgrade() -> None:
    op.drop_table("trend_activation_decisions")
    op.drop_table("trend_activation_runs")
    op.drop_table("trend_activation_policy_versions")
