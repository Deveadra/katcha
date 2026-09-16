"""add ranked episode render and publication lineage

Revision ID: 0019_ranked_episode_render_publishing
Revises: 0018_discovery_trend_review_queue
Create Date: 2026-09-16
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0019_ranked_episode_render_publishing"
down_revision: str | None = "0018_discovery_trend_review_queue"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "short_episodes",
        sa.Column("trend_opportunity_id", sa.Uuid(), nullable=True),
    )
    op.add_column(
        "short_episodes",
        sa.Column("render_manifest", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
    )
    op.create_index(
        "ix_short_episodes_trend_opportunity_id",
        "short_episodes",
        ["trend_opportunity_id"],
    )
    op.create_foreign_key(
        "fk_short_episodes_trend_opportunity_id",
        "short_episodes",
        "trend_opportunities",
        ["trend_opportunity_id"],
        ["id"],
    )

    op.add_column(
        "publications",
        sa.Column("short_episode_id", sa.Uuid(), nullable=True),
    )
    op.add_column(
        "publications",
        sa.Column("treatment_metadata", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
    )
    op.create_index(
        "ix_publications_short_episode_id",
        "publications",
        ["short_episode_id"],
    )
    op.create_foreign_key(
        "fk_publications_short_episode_id",
        "publications",
        "short_episodes",
        ["short_episode_id"],
        ["id"],
    )
    op.create_unique_constraint(
        "uq_publications_short_episode_youtube_connection",
        "publications",
        ["short_episode_id", "youtube_connection_id"],
    )
    op.drop_constraint(
        "ck_publications_exactly_one_source",
        "publications",
        type_="check",
    )
    op.create_check_constraint(
        "ck_publications_exactly_one_source",
        "publications",
        "(production_id IS NOT NULL AND compilation_id IS NULL AND short_episode_id IS NULL) OR "
        "(production_id IS NULL AND compilation_id IS NOT NULL AND short_episode_id IS NULL) OR "
        "(production_id IS NULL AND compilation_id IS NULL AND short_episode_id IS NOT NULL)",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_publications_exactly_one_source",
        "publications",
        type_="check",
    )
    op.create_check_constraint(
        "ck_publications_exactly_one_source",
        "publications",
        "(production_id IS NOT NULL AND compilation_id IS NULL) OR "
        "(production_id IS NULL AND compilation_id IS NOT NULL)",
    )
    op.drop_constraint(
        "uq_publications_short_episode_youtube_connection",
        "publications",
        type_="unique",
    )
    op.drop_constraint(
        "fk_publications_short_episode_id",
        "publications",
        type_="foreignkey",
    )
    op.drop_index("ix_publications_short_episode_id", table_name="publications")
    op.drop_column("publications", "treatment_metadata")
    op.drop_column("publications", "short_episode_id")

    op.drop_constraint(
        "fk_short_episodes_trend_opportunity_id",
        "short_episodes",
        type_="foreignkey",
    )
    op.drop_index(
        "ix_short_episodes_trend_opportunity_id",
        table_name="short_episodes",
    )
    op.drop_column("short_episodes", "render_manifest")
    op.drop_column("short_episodes", "trend_opportunity_id")
