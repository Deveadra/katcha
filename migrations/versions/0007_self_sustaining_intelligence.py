"""self-sustaining channel intelligence foundation

Revision ID: 0007_self_sustaining_intelligence
Revises: 0006_compilation_publications
Create Date: 2026-09-15
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007_self_sustaining_intelligence"
down_revision: str | None = "0006_compilation_publications"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "channel_profiles",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("youtube_connection_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("timezone", sa.String(length=128), nullable=False),
        sa.Column("active_strategy_version", sa.Integer(), nullable=False),
        sa.Column("active_automation_version", sa.Integer(), nullable=False),
        sa.Column("profile_metadata", sa.JSON(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["youtube_connection_id"],
            ["youtube_connections.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("youtube_connection_id"),
    )
    op.create_index("ix_channel_profiles_youtube_connection_id", "channel_profiles", ["youtube_connection_id"])
    op.create_index("ix_channel_profiles_status", "channel_profiles", ["status"])

    op.create_table(
        "channel_strategy_versions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("channel_profile_id", sa.Uuid(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("monthly_hard_budget_usd", sa.Numeric(14, 4), nullable=False),
        sa.Column("reinvestment_rate", sa.Numeric(8, 6), nullable=False),
        sa.Column("reinvestment_cap_usd", sa.Numeric(14, 4), nullable=False),
        sa.Column("fallback_schedule", sa.JSON(), nullable=False),
        sa.Column("blackout_windows", sa.JSON(), nullable=False),
        sa.Column("routing_policy", sa.JSON(), nullable=False),
        sa.Column("strategy_metadata", sa.JSON(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("monthly_hard_budget_usd >= 0", name="ck_strategy_budget_nonnegative"),
        sa.CheckConstraint(
            "reinvestment_rate >= 0 AND reinvestment_rate <= 1",
            name="ck_strategy_reinvestment_rate",
        ),
        sa.CheckConstraint("reinvestment_cap_usd >= 0", name="ck_strategy_reinvestment_cap"),
        sa.ForeignKeyConstraint(["channel_profile_id"], ["channel_profiles.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("channel_profile_id", "version"),
    )
    op.create_index(
        "ix_channel_strategy_versions_channel_profile_id",
        "channel_strategy_versions",
        ["channel_profile_id"],
    )

    op.create_table(
        "automation_policy_versions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("channel_profile_id", sa.Uuid(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("level", sa.String(length=64), nullable=False),
        sa.Column("min_reviewed_items", sa.Integer(), nullable=False),
        sa.Column("min_approval_rate", sa.Numeric(8, 6), nullable=False),
        sa.Column("max_regeneration_rate", sa.Numeric(8, 6), nullable=False),
        sa.Column("max_publication_failure_rate", sa.Numeric(8, 6), nullable=False),
        sa.Column("min_ranking_confidence", sa.Numeric(8, 6), nullable=False),
        sa.Column("auto_demote", sa.Boolean(), nullable=False),
        sa.Column("policy_metadata", sa.JSON(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("min_reviewed_items >= 0", name="ck_automation_min_reviewed"),
        sa.CheckConstraint(
            "min_approval_rate >= 0 AND min_approval_rate <= 1",
            name="ck_automation_approval_rate",
        ),
        sa.CheckConstraint(
            "max_regeneration_rate >= 0 AND max_regeneration_rate <= 1",
            name="ck_automation_regeneration_rate",
        ),
        sa.CheckConstraint(
            "max_publication_failure_rate >= 0 AND max_publication_failure_rate <= 1",
            name="ck_automation_publication_failure_rate",
        ),
        sa.CheckConstraint(
            "min_ranking_confidence >= 0 AND min_ranking_confidence <= 1",
            name="ck_automation_ranking_confidence",
        ),
        sa.ForeignKeyConstraint(["channel_profile_id"], ["channel_profiles.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("channel_profile_id", "version"),
    )
    op.create_index(
        "ix_automation_policy_versions_channel_profile_id",
        "automation_policy_versions",
        ["channel_profile_id"],
    )
    op.create_index("ix_automation_policy_versions_level", "automation_policy_versions", ["level"])

    op.create_table(
        "performance_observations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("channel_profile_id", sa.Uuid(), nullable=False),
        sa.Column("publication_id", sa.Uuid(), nullable=False),
        sa.Column("analytics_snapshot_id", sa.Uuid(), nullable=False),
        sa.Column("source_kind", sa.String(length=32), nullable=False),
        sa.Column("source_id", sa.Uuid(), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("publication_age_hours", sa.Numeric(14, 4), nullable=False),
        sa.Column("features", sa.JSON(), nullable=False),
        sa.Column("labels", sa.JSON(), nullable=False),
        sa.Column("outcome_score", sa.Numeric(8, 6), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["analytics_snapshot_id"], ["publication_analytics_snapshots.id"]),
        sa.ForeignKeyConstraint(["channel_profile_id"], ["channel_profiles.id"]),
        sa.ForeignKeyConstraint(["publication_id"], ["publications.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("analytics_snapshot_id"),
    )
    op.create_index(
        "ix_performance_observations_channel_profile_id",
        "performance_observations",
        ["channel_profile_id"],
    )
    op.create_index("ix_performance_observations_publication_id", "performance_observations", ["publication_id"])
    op.create_index(
        "ix_performance_observations_analytics_snapshot_id",
        "performance_observations",
        ["analytics_snapshot_id"],
    )
    op.create_index("ix_performance_observations_source_kind", "performance_observations", ["source_kind"])
    op.create_index("ix_performance_observations_source_id", "performance_observations", ["source_id"])
    op.create_index("ix_performance_observations_observed_at", "performance_observations", ["observed_at"])
    op.create_index("ix_performance_observations_outcome_score", "performance_observations", ["outcome_score"])

    op.create_table(
        "ranking_snapshots",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("channel_profile_id", sa.Uuid(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("algorithm", sa.String(length=64), nullable=False),
        sa.Column("training_cutoff", sa.DateTime(timezone=True), nullable=False),
        sa.Column("sample_count", sa.Integer(), nullable=False),
        sa.Column("feature_names", sa.JSON(), nullable=False),
        sa.Column("feature_means", sa.JSON(), nullable=False),
        sa.Column("feature_scales", sa.JSON(), nullable=False),
        sa.Column("coefficients", sa.JSON(), nullable=False),
        sa.Column("intercept", sa.Numeric(14, 8), nullable=False),
        sa.Column("blend_ratio", sa.Numeric(8, 6), nullable=False),
        sa.Column("confidence", sa.Numeric(8, 6), nullable=False),
        sa.Column("validation_metrics", sa.JSON(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("sample_count >= 0", name="ck_ranking_sample_count"),
        sa.CheckConstraint("blend_ratio >= 0 AND blend_ratio <= 1", name="ck_ranking_blend_ratio"),
        sa.CheckConstraint("confidence >= 0 AND confidence <= 1", name="ck_ranking_confidence"),
        sa.ForeignKeyConstraint(["channel_profile_id"], ["channel_profiles.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("channel_profile_id", "version"),
    )
    op.create_index("ix_ranking_snapshots_channel_profile_id", "ranking_snapshots", ["channel_profile_id"])
    op.create_index("ix_ranking_snapshots_training_cutoff", "ranking_snapshots", ["training_cutoff"])

    op.create_table(
        "channel_economics_snapshots",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("channel_profile_id", sa.Uuid(), nullable=False),
        sa.Column("sample_key", sa.String(length=128), nullable=False),
        sa.Column("sampled_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revenue_usd", sa.Numeric(18, 8), nullable=False),
        sa.Column("attributed_ai_cost_usd", sa.Numeric(18, 8), nullable=False),
        sa.Column("contribution_margin_usd", sa.Numeric(18, 8), nullable=False),
        sa.Column("reinvestable_usd", sa.Numeric(18, 8), nullable=False),
        sa.Column("hard_budget_usd", sa.Numeric(18, 8), nullable=False),
        sa.Column("month_to_date_spend_usd", sa.Numeric(18, 8), nullable=False),
        sa.Column("budget_headroom_usd", sa.Numeric(18, 8), nullable=False),
        sa.Column("monetary_scope_available", sa.Boolean(), nullable=False),
        sa.Column("details", sa.JSON(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["channel_profile_id"], ["channel_profiles.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("channel_profile_id", "sample_key"),
    )
    op.create_index(
        "ix_channel_economics_snapshots_channel_profile_id",
        "channel_economics_snapshots",
        ["channel_profile_id"],
    )
    op.create_index("ix_channel_economics_snapshots_sampled_at", "channel_economics_snapshots", ["sampled_at"])

    op.create_table(
        "schedule_recommendations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("channel_profile_id", sa.Uuid(), nullable=False),
        sa.Column("run_key", sa.String(length=128), nullable=False),
        sa.Column("rank", sa.Integer(), nullable=False),
        sa.Column("weekday", sa.Integer(), nullable=False),
        sa.Column("hour_local", sa.Integer(), nullable=False),
        sa.Column("score", sa.Numeric(8, 6), nullable=False),
        sa.Column("sample_count", sa.Integer(), nullable=False),
        sa.Column("confidence", sa.Numeric(8, 6), nullable=False),
        sa.Column("source", sa.String(length=64), nullable=False),
        sa.Column("recommendation_metadata", sa.JSON(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("weekday >= 0 AND weekday <= 6", name="ck_schedule_weekday"),
        sa.CheckConstraint("hour_local >= 0 AND hour_local <= 23", name="ck_schedule_hour"),
        sa.CheckConstraint("sample_count >= 0", name="ck_schedule_sample_count"),
        sa.CheckConstraint("confidence >= 0 AND confidence <= 1", name="ck_schedule_confidence"),
        sa.ForeignKeyConstraint(["channel_profile_id"], ["channel_profiles.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("channel_profile_id", "run_key", "rank"),
    )
    op.create_index(
        "ix_schedule_recommendations_channel_profile_id",
        "schedule_recommendations",
        ["channel_profile_id"],
    )
    op.create_index("ix_schedule_recommendations_run_key", "schedule_recommendations", ["run_key"])

    op.create_table(
        "event_consumer_cursors",
        sa.Column("consumer_key", sa.String(length=128), nullable=False),
        sa.Column("last_event_id", sa.Uuid(), nullable=True),
        sa.Column("last_event_created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cursor_metadata", sa.JSON(), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("consumer_key"),
    )
    op.create_index(
        "ix_event_consumer_cursors_last_event_created_at",
        "event_consumer_cursors",
        ["last_event_created_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_event_consumer_cursors_last_event_created_at",
        table_name="event_consumer_cursors",
    )
    op.drop_table("event_consumer_cursors")
    op.drop_index("ix_schedule_recommendations_run_key", table_name="schedule_recommendations")
    op.drop_index(
        "ix_schedule_recommendations_channel_profile_id",
        table_name="schedule_recommendations",
    )
    op.drop_table("schedule_recommendations")
    op.drop_index(
        "ix_channel_economics_snapshots_sampled_at",
        table_name="channel_economics_snapshots",
    )
    op.drop_index(
        "ix_channel_economics_snapshots_channel_profile_id",
        table_name="channel_economics_snapshots",
    )
    op.drop_table("channel_economics_snapshots")
    op.drop_index("ix_ranking_snapshots_training_cutoff", table_name="ranking_snapshots")
    op.drop_index("ix_ranking_snapshots_channel_profile_id", table_name="ranking_snapshots")
    op.drop_table("ranking_snapshots")
    op.drop_index("ix_performance_observations_outcome_score", table_name="performance_observations")
    op.drop_index("ix_performance_observations_observed_at", table_name="performance_observations")
    op.drop_index("ix_performance_observations_source_id", table_name="performance_observations")
    op.drop_index("ix_performance_observations_source_kind", table_name="performance_observations")
    op.drop_index(
        "ix_performance_observations_analytics_snapshot_id",
        table_name="performance_observations",
    )
    op.drop_index("ix_performance_observations_publication_id", table_name="performance_observations")
    op.drop_index(
        "ix_performance_observations_channel_profile_id",
        table_name="performance_observations",
    )
    op.drop_table("performance_observations")
    op.drop_index("ix_automation_policy_versions_level", table_name="automation_policy_versions")
    op.drop_index(
        "ix_automation_policy_versions_channel_profile_id",
        table_name="automation_policy_versions",
    )
    op.drop_table("automation_policy_versions")
    op.drop_index(
        "ix_channel_strategy_versions_channel_profile_id",
        table_name="channel_strategy_versions",
    )
    op.drop_table("channel_strategy_versions")
    op.drop_index("ix_channel_profiles_status", table_name="channel_profiles")
    op.drop_index("ix_channel_profiles_youtube_connection_id", table_name="channel_profiles")
    op.drop_table("channel_profiles")
