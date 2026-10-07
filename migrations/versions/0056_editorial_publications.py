"""add approved editorial render publication lineage

Revision ID: 0056_editorial_publications
Revises: 0055_editorial_storyboards
Create Date: 2026-10-07
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0056_editorial_publications"
down_revision: str | None = "0055_editorial_storyboards"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "publications",
        sa.Column("editorial_run_id", sa.Uuid(), nullable=True),
    )
    op.create_index(
        "ix_publications_editorial_run_id",
        "publications",
        ["editorial_run_id"],
    )
    op.create_foreign_key(
        "fk_publications_editorial_run_id",
        "publications",
        "editorial_runs",
        ["editorial_run_id"],
        ["id"],
    )
    op.create_unique_constraint(
        "uq_publications_editorial_run_youtube_connection",
        "publications",
        ["editorial_run_id", "youtube_connection_id"],
    )
    op.drop_constraint(
        "ck_publications_exactly_one_source",
        "publications",
        type_="check",
    )
    op.create_check_constraint(
        "ck_publications_exactly_one_source",
        "publications",
        "(production_id IS NOT NULL AND compilation_id IS NULL "
        "AND short_episode_id IS NULL AND editorial_run_id IS NULL) OR "
        "(production_id IS NULL AND compilation_id IS NOT NULL "
        "AND short_episode_id IS NULL AND editorial_run_id IS NULL) OR "
        "(production_id IS NULL AND compilation_id IS NULL "
        "AND short_episode_id IS NOT NULL AND editorial_run_id IS NULL) OR "
        "(production_id IS NULL AND compilation_id IS NULL "
        "AND short_episode_id IS NULL AND editorial_run_id IS NOT NULL)",
    )


def downgrade() -> None:
    op.execute("DELETE FROM publications WHERE editorial_run_id IS NOT NULL")
    op.drop_constraint(
        "ck_publications_exactly_one_source",
        "publications",
        type_="check",
    )
    op.create_check_constraint(
        "ck_publications_exactly_one_source",
        "publications",
        "(production_id IS NOT NULL AND compilation_id IS NULL "
        "AND short_episode_id IS NULL) OR "
        "(production_id IS NULL AND compilation_id IS NOT NULL "
        "AND short_episode_id IS NULL) OR "
        "(production_id IS NULL AND compilation_id IS NULL "
        "AND short_episode_id IS NOT NULL)",
    )
    op.drop_constraint(
        "uq_publications_editorial_run_youtube_connection",
        "publications",
        type_="unique",
    )
    op.drop_constraint(
        "fk_publications_editorial_run_id",
        "publications",
        type_="foreignkey",
    )
    op.drop_index("ix_publications_editorial_run_id", table_name="publications")
    op.drop_column("publications", "editorial_run_id")
