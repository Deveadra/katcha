"""add topic watches and candidate trend scores

Revision ID: 0015_trend_watches
Revises: 0014_short_episodes
Create Date: 2026-09-16
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0015_trend_watches"
down_revision: str | None = "0014_short_episodes"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "topic_watch_versions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("watch_key", sa.String(length=128), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("include_terms", sa.JSON(), nullable=False),
        sa.Column("exclude_terms", sa.JSON(), nullable=False),
        sa.Column("adapter_configs", sa.JSON(), nullable=False),
        sa.Column("language", sa.String(length=32), nullable=True),
        sa.Column("locale", sa.String(length=64), nullable=True),
        sa.Column("freshness_horizon_hours", sa.Integer(), nullable=False),
        sa.Column("max_candidates", sa.Integer(), nullable=False),
        sa.Column("watch_metadata", sa.JSON(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("version > 0", name="ck_topic_watch_version_positive"),
        sa.CheckConstraint(
            "freshness_horizon_hours > 0",
            name="ck_topic_watch_freshness_positive",
        ),
        sa.CheckConstraint(
            "max_candidates > 0",
            name="ck_topic_watch_max_candidates_positive",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("watch_key", "version"),
    )
    op.create_index(
        "ix_topic_watch_versions_watch_key",
        "topic_watch_versions",
        ["watch_key"],
    )
    op.create_index(
        "ix_topic_watch_versions_enabled",
        "topic_watch_versions",
        ["enabled"],
    )
    op.create_index(
        "ix_topic_watch_versions_created_at",
        "topic_watch_versions",
        ["created_at"],
    )

    op.create_table(
        "candidate_trend_scores",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("topic_watch_id", sa.Uuid(), nullable=False),
        sa.Column("discovery_candidate_id", sa.Uuid(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("score_key", sa.String(length=160), nullable=False),
        sa.Column("algorithm_version", sa.String(length=64), nullable=False),
        sa.Column("score", sa.Numeric(8, 6), nullable=False),
        sa.Column("feature_breakdown", sa.JSON(), nullable=False),
        sa.Column("observation_count", sa.Integer(), nullable=False),
        sa.Column("window_start", sa.DateTime(timezone=True), nullable=True),
        sa.Column("window_end", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "computed_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("version > 0", name="ck_trend_score_version_positive"),
        sa.CheckConstraint(
            "score >= 0 AND score <= 1",
            name="ck_trend_score_probability_range",
        ),
        sa.ForeignKeyConstraint(
            ["topic_watch_id"],
            ["topic_watch_versions.id"],
        ),
        sa.ForeignKeyConstraint(
            ["discovery_candidate_id"],
            ["discovery_candidates.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "topic_watch_id",
            "discovery_candidate_id",
            "version",
        ),
        sa.UniqueConstraint(
            "topic_watch_id",
            "discovery_candidate_id",
            "score_key",
        ),
    )
    op.create_index(
        "ix_candidate_trend_scores_topic_watch_id",
        "candidate_trend_scores",
        ["topic_watch_id"],
    )
    op.create_index(
        "ix_candidate_trend_scores_discovery_candidate_id",
        "candidate_trend_scores",
        ["discovery_candidate_id"],
    )
    op.create_index(
        "ix_candidate_trend_scores_algorithm_version",
        "candidate_trend_scores",
        ["algorithm_version"],
    )
    op.create_index(
        "ix_candidate_trend_scores_score",
        "candidate_trend_scores",
        ["score"],
    )
    op.create_index(
        "ix_candidate_trend_scores_computed_at",
        "candidate_trend_scores",
        ["computed_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_candidate_trend_scores_computed_at",
        table_name="candidate_trend_scores",
    )
    op.drop_index(
        "ix_candidate_trend_scores_score",
        table_name="candidate_trend_scores",
    )
    op.drop_index(
        "ix_candidate_trend_scores_algorithm_version",
        table_name="candidate_trend_scores",
    )
    op.drop_index(
        "ix_candidate_trend_scores_discovery_candidate_id",
        table_name="candidate_trend_scores",
    )
    op.drop_index(
        "ix_candidate_trend_scores_topic_watch_id",
        table_name="candidate_trend_scores",
    )
    op.drop_table("candidate_trend_scores")

    op.drop_index(
        "ix_topic_watch_versions_created_at",
        table_name="topic_watch_versions",
    )
    op.drop_index(
        "ix_topic_watch_versions_enabled",
        table_name="topic_watch_versions",
    )
    op.drop_index(
        "ix_topic_watch_versions_watch_key",
        table_name="topic_watch_versions",
    )
    op.drop_table("topic_watch_versions")
