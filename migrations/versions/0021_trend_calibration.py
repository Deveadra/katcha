"""add trend outcome calibration

Revision ID: 0021_trend_calibration
Revises: 0020_merge_ranked_trend_heads
Create Date: 2026-09-23
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0021_trend_calibration"
down_revision: str | None = "0020_merge_ranked_trend_heads"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "trend_opportunities",
        sa.Column("calibrated_score", sa.Numeric(8, 6), nullable=True),
    )
    op.add_column(
        "trend_opportunities",
        sa.Column("calibration_version", sa.Integer(), nullable=True),
    )
    op.add_column(
        "trend_opportunities",
        sa.Column(
            "calibration_metadata",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
    )
    op.create_check_constraint(
        "ck_trend_opportunity_calibrated_score",
        "trend_opportunities",
        "calibrated_score IS NULL OR "
        "(calibrated_score >= 0 AND calibrated_score <= 1)",
    )
    op.create_index(
        "ix_trend_opportunities_calibrated_score",
        "trend_opportunities",
        ["calibrated_score"],
    )

    op.create_table(
        "trend_outcome_attributions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("channel_profile_id", sa.Uuid(), nullable=False),
        sa.Column("publication_id", sa.Uuid(), nullable=False),
        sa.Column("analytics_snapshot_id", sa.Uuid(), nullable=False),
        sa.Column("trend_opportunity_id", sa.Uuid(), nullable=True),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("source_kind", sa.String(32), nullable=False),
        sa.Column("attribution_metadata", sa.JSON(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["channel_profile_id"], ["channel_profiles.id"]),
        sa.ForeignKeyConstraint(["publication_id"], ["publications.id"]),
        sa.ForeignKeyConstraint(
            ["analytics_snapshot_id"], ["publication_analytics_snapshots.id"]
        ),
        sa.ForeignKeyConstraint(
            ["trend_opportunity_id"], ["trend_opportunities.id"]
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("analytics_snapshot_id"),
    )
    for column in (
        "channel_profile_id",
        "publication_id",
        "analytics_snapshot_id",
        "trend_opportunity_id",
        "status",
        "source_kind",
        "created_at",
    ):
        op.create_index(
            f"ix_trend_outcome_attributions_{column}",
            "trend_outcome_attributions",
            [column],
        )

    op.create_table(
        "trend_opportunity_outcomes",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("channel_profile_id", sa.Uuid(), nullable=False),
        sa.Column("trend_opportunity_id", sa.Uuid(), nullable=False),
        sa.Column("publication_id", sa.Uuid(), nullable=False),
        sa.Column("analytics_snapshot_id", sa.Uuid(), nullable=False),
        sa.Column("age_bucket_hours", sa.Integer(), nullable=False),
        sa.Column("opportunity_created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("sampled_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("lead_time_hours", sa.Numeric(14, 4), nullable=False),
        sa.Column("predicted_score", sa.Numeric(8, 6), nullable=False),
        sa.Column("predicted_confidence", sa.Numeric(8, 6), nullable=False),
        sa.Column("predicted_lifecycle", sa.String(32), nullable=False),
        sa.Column("prediction_components", sa.JSON(), nullable=False),
        sa.Column("observed_outcome_score", sa.Numeric(8, 6), nullable=False),
        sa.Column("baseline_sample_count", sa.Integer(), nullable=False),
        sa.Column("baseline_outcome_score", sa.Numeric(8, 6), nullable=True),
        sa.Column("lift_ratio", sa.Numeric(14, 6), nullable=True),
        sa.Column("realized_breakout", sa.Boolean(), nullable=True),
        sa.Column("outcome_metrics", sa.JSON(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "age_bucket_hours > 0", name="ck_trend_outcome_age_bucket_positive"
        ),
        sa.CheckConstraint(
            "predicted_score >= 0 AND predicted_score <= 1",
            name="ck_trend_outcome_predicted_score",
        ),
        sa.CheckConstraint(
            "predicted_confidence >= 0 AND predicted_confidence <= 1",
            name="ck_trend_outcome_predicted_confidence",
        ),
        sa.CheckConstraint(
            "observed_outcome_score >= 0 AND observed_outcome_score <= 1",
            name="ck_trend_outcome_observed_score",
        ),
        sa.CheckConstraint(
            "baseline_sample_count >= 0",
            name="ck_trend_outcome_baseline_samples_nonnegative",
        ),
        sa.ForeignKeyConstraint(["channel_profile_id"], ["channel_profiles.id"]),
        sa.ForeignKeyConstraint(
            ["trend_opportunity_id"], ["trend_opportunities.id"]
        ),
        sa.ForeignKeyConstraint(["publication_id"], ["publications.id"]),
        sa.ForeignKeyConstraint(
            ["analytics_snapshot_id"], ["publication_analytics_snapshots.id"]
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("analytics_snapshot_id"),
    )
    for column in (
        "channel_profile_id",
        "trend_opportunity_id",
        "publication_id",
        "analytics_snapshot_id",
        "age_bucket_hours",
        "opportunity_created_at",
        "published_at",
        "sampled_at",
        "predicted_lifecycle",
        "observed_outcome_score",
        "realized_breakout",
        "created_at",
    ):
        op.create_index(
            f"ix_trend_opportunity_outcomes_{column}",
            "trend_opportunity_outcomes",
            [column],
        )

    op.create_table(
        "trend_calibration_snapshots",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("channel_profile_id", sa.Uuid(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("run_key", sa.String(160), nullable=False),
        sa.Column("algorithm", sa.String(64), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("training_cutoff", sa.DateTime(timezone=True), nullable=False),
        sa.Column("sample_count", sa.Integer(), nullable=False),
        sa.Column("training_sample_count", sa.Integer(), nullable=False),
        sa.Column("validation_sample_count", sa.Integer(), nullable=False),
        sa.Column("feature_names", sa.JSON(), nullable=False),
        sa.Column("feature_means", sa.JSON(), nullable=False),
        sa.Column("feature_scales", sa.JSON(), nullable=False),
        sa.Column("coefficients", sa.JSON(), nullable=False),
        sa.Column("intercept", sa.Numeric(14, 8), nullable=False),
        sa.Column("blend_ratio", sa.Numeric(8, 6), nullable=False),
        sa.Column("confidence", sa.Numeric(8, 6), nullable=False),
        sa.Column("validation_metrics", sa.JSON(), nullable=False),
        sa.Column("calibration_metrics", sa.JSON(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "sample_count >= 0",
            name="ck_trend_calibration_sample_count_nonnegative",
        ),
        sa.CheckConstraint(
            "training_sample_count >= 0",
            name="ck_trend_calibration_training_count_nonnegative",
        ),
        sa.CheckConstraint(
            "validation_sample_count >= 0",
            name="ck_trend_calibration_validation_count_nonnegative",
        ),
        sa.CheckConstraint(
            "blend_ratio >= 0 AND blend_ratio <= 1",
            name="ck_trend_calibration_blend_ratio",
        ),
        sa.CheckConstraint(
            "confidence >= 0 AND confidence <= 1",
            name="ck_trend_calibration_confidence",
        ),
        sa.ForeignKeyConstraint(["channel_profile_id"], ["channel_profiles.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("channel_profile_id", "version"),
        sa.UniqueConstraint("channel_profile_id", "run_key"),
    )
    for column in (
        "channel_profile_id",
        "run_key",
        "status",
        "training_cutoff",
        "created_at",
    ):
        op.create_index(
            f"ix_trend_calibration_snapshots_{column}",
            "trend_calibration_snapshots",
            [column],
        )


def downgrade() -> None:
    op.drop_table("trend_calibration_snapshots")
    op.drop_table("trend_opportunity_outcomes")
    op.drop_table("trend_outcome_attributions")
    op.drop_index(
        "ix_trend_opportunities_calibrated_score",
        table_name="trend_opportunities",
    )
    op.drop_constraint(
        "ck_trend_opportunity_calibrated_score",
        "trend_opportunities",
        type_="check",
    )
    op.drop_column("trend_opportunities", "calibration_metadata")
    op.drop_column("trend_opportunities", "calibration_version")
    op.drop_column("trend_opportunities", "calibrated_score")
