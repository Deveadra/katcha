"""add packaging intelligence snapshots

Revision ID: 0030_packaging_intelligence
Revises: 0029_packaging_reach
Create Date: 2026-09-24
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0030_packaging_intelligence"
down_revision: str | None = "0029_packaging_reach"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "packaging_variant_performance_windows",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("publication_id", sa.Uuid(), nullable=False),
        sa.Column("packaging_variant_id", sa.Uuid(), nullable=False),
        sa.Column("evidence_key", sa.String(length=64), nullable=False),
        sa.Column("maturity_days", sa.Integer(), nullable=False),
        sa.Column("window_start", sa.Date(), nullable=False),
        sa.Column("window_end", sa.Date(), nullable=False),
        sa.Column("impressions", sa.Integer(), nullable=True),
        sa.Column("ctr", sa.Numeric(12, 8), nullable=True),
        sa.Column("views", sa.Integer(), nullable=True),
        sa.Column("average_view_percentage", sa.Numeric(12, 6), nullable=True),
        sa.Column("retention_50", sa.Numeric(12, 8), nullable=True),
        sa.Column("interval_revenue_usd", sa.Numeric(18, 8), nullable=True),
        sa.Column("publication_cost_usd", sa.Numeric(18, 8), nullable=False),
        sa.Column("publication_revenue_usd", sa.Numeric(18, 8), nullable=True),
        sa.Column(
            "publication_contribution_margin_usd",
            sa.Numeric(18, 8),
            nullable=True,
        ),
        sa.Column("evidence_status", sa.String(length=64), nullable=False),
        sa.Column("evidence_metadata", sa.JSON(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "maturity_days > 0",
            name="ck_packaging_window_maturity_positive",
        ),
        sa.CheckConstraint(
            "window_end >= window_start",
            name="ck_packaging_window_dates",
        ),
        sa.CheckConstraint(
            "impressions IS NULL OR impressions >= 0",
            name="ck_packaging_window_impressions_nonnegative",
        ),
        sa.CheckConstraint(
            "ctr IS NULL OR (ctr >= 0 AND ctr <= 1)",
            name="ck_packaging_window_ctr",
        ),
        sa.ForeignKeyConstraint(
            ["publication_id"],
            ["publications.id"],
        ),
        sa.ForeignKeyConstraint(
            ["packaging_variant_id"],
            ["publication_packaging_variants.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "packaging_variant_id",
            "evidence_key",
            name="uq_packaging_window_variant_evidence",
        ),
    )
    for column in (
        "publication_id",
        "packaging_variant_id",
        "evidence_key",
        "maturity_days",
        "window_start",
        "window_end",
        "evidence_status",
    ):
        op.create_index(
            f"ix_packaging_variant_performance_windows_{column}",
            "packaging_variant_performance_windows",
            [column],
        )

    op.create_table(
        "packaging_intelligence_snapshots",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("channel_profile_id", sa.Uuid(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("run_key", sa.String(length=160), nullable=False),
        sa.Column("maturity_days", sa.Integer(), nullable=False),
        sa.Column("publication_count", sa.Integer(), nullable=False),
        sa.Column("variant_window_count", sa.Integer(), nullable=False),
        sa.Column("recommendation_count", sa.Integer(), nullable=False),
        sa.Column("recommendation_status", sa.String(length=64), nullable=False),
        sa.Column("variant_metrics", sa.JSON(), nullable=False),
        sa.Column("recommendations", sa.JSON(), nullable=False),
        sa.Column("validation_metrics", sa.JSON(), nullable=False),
        sa.Column("policy_snapshot", sa.JSON(), nullable=False),
        sa.Column("sample_window_start", sa.Date(), nullable=True),
        sa.Column("sample_window_end", sa.Date(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "maturity_days > 0",
            name="ck_packaging_intelligence_maturity_positive",
        ),
        sa.CheckConstraint(
            "publication_count >= 0",
            name="ck_packaging_intelligence_publications_nonnegative",
        ),
        sa.CheckConstraint(
            "variant_window_count >= 0",
            name="ck_packaging_intelligence_windows_nonnegative",
        ),
        sa.CheckConstraint(
            "recommendation_count >= 0",
            name="ck_packaging_intelligence_recommendations_nonnegative",
        ),
        sa.ForeignKeyConstraint(
            ["channel_profile_id"],
            ["channel_profiles.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("channel_profile_id", "version"),
        sa.UniqueConstraint("channel_profile_id", "run_key"),
    )
    for column in (
        "channel_profile_id",
        "run_key",
        "maturity_days",
        "recommendation_status",
    ):
        op.create_index(
            f"ix_packaging_intelligence_snapshots_{column}",
            "packaging_intelligence_snapshots",
            [column],
        )


def downgrade() -> None:
    op.drop_table("packaging_intelligence_snapshots")
    op.drop_table("packaging_variant_performance_windows")
