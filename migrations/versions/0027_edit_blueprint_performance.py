"""add edit blueprint performance snapshots

Revision ID: 0027_edit_blueprint_performance
Revises: 0026_render_attempts
Create Date: 2026-09-24
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0027_edit_blueprint_performance"
down_revision: str | None = "0026_render_attempts"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "edit_blueprint_performance_snapshots",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("channel_profile_id", sa.Uuid(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("run_key", sa.String(length=160), nullable=False),
        sa.Column("age_bucket_hours", sa.Integer(), nullable=False),
        sa.Column("publication_count", sa.Integer(), nullable=False),
        sa.Column("blueprint_group_count", sa.Integer(), nullable=False),
        sa.Column("revenue_covered_publications", sa.Integer(), nullable=False),
        sa.Column("retention_covered_publications", sa.Integer(), nullable=False),
        sa.Column("monetary_coverage", sa.Numeric(8, 6), nullable=False),
        sa.Column("retention_coverage", sa.Numeric(8, 6), nullable=False),
        sa.Column("aggregate_metrics", sa.JSON(), nullable=False),
        sa.Column("comparison_status", sa.String(length=64), nullable=False),
        sa.Column("comparison_summary", sa.JSON(), nullable=False),
        sa.Column("sample_window_start", sa.DateTime(timezone=True), nullable=True),
        sa.Column("sample_window_end", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "age_bucket_hours > 0",
            name="ck_edit_perf_age_bucket_positive",
        ),
        sa.CheckConstraint(
            "publication_count >= 0",
            name="ck_edit_perf_publication_count_nonnegative",
        ),
        sa.CheckConstraint(
            "blueprint_group_count >= 0",
            name="ck_edit_perf_group_count_nonnegative",
        ),
        sa.CheckConstraint(
            "revenue_covered_publications >= 0",
            name="ck_edit_perf_revenue_covered_nonnegative",
        ),
        sa.CheckConstraint(
            "retention_covered_publications >= 0",
            name="ck_edit_perf_retention_covered_nonnegative",
        ),
        sa.CheckConstraint(
            "monetary_coverage >= 0 AND monetary_coverage <= 1",
            name="ck_edit_perf_monetary_coverage",
        ),
        sa.CheckConstraint(
            "retention_coverage >= 0 AND retention_coverage <= 1",
            name="ck_edit_perf_retention_coverage",
        ),
        sa.ForeignKeyConstraint(["channel_profile_id"], ["channel_profiles.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("channel_profile_id", "version"),
        sa.UniqueConstraint("channel_profile_id", "run_key"),
    )
    op.create_index(
        op.f("ix_edit_blueprint_performance_snapshots_age_bucket_hours"),
        "edit_blueprint_performance_snapshots",
        ["age_bucket_hours"],
        unique=False,
    )
    op.create_index(
        op.f("ix_edit_blueprint_performance_snapshots_channel_profile_id"),
        "edit_blueprint_performance_snapshots",
        ["channel_profile_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_edit_blueprint_performance_snapshots_comparison_status"),
        "edit_blueprint_performance_snapshots",
        ["comparison_status"],
        unique=False,
    )
    op.create_index(
        op.f("ix_edit_blueprint_performance_snapshots_run_key"),
        "edit_blueprint_performance_snapshots",
        ["run_key"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_edit_blueprint_performance_snapshots_run_key"),
        table_name="edit_blueprint_performance_snapshots",
    )
    op.drop_index(
        op.f("ix_edit_blueprint_performance_snapshots_age_bucket_hours"),
        table_name="edit_blueprint_performance_snapshots",
    )
    op.drop_index(
        op.f("ix_edit_blueprint_performance_snapshots_comparison_status"),
        table_name="edit_blueprint_performance_snapshots",
    )
    op.drop_index(
        op.f("ix_edit_blueprint_performance_snapshots_channel_profile_id"),
        table_name="edit_blueprint_performance_snapshots",
    )
    op.drop_table("edit_blueprint_performance_snapshots")
