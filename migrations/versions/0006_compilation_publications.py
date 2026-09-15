"""allow compilation publications

Revision ID: 0006_compilation_publications
Revises: 0005_longform_compiler
Create Date: 2026-09-15
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006_compilation_publications"
down_revision: str | None = "0005_longform_compiler"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column("publications", "production_id", existing_type=sa.Uuid(), nullable=True)
    op.add_column("publications", sa.Column("compilation_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        "fk_publications_compilation_id_compilations",
        "publications",
        "compilations",
        ["compilation_id"],
        ["id"],
    )
    op.create_index(
        "ix_publications_compilation_id",
        "publications",
        ["compilation_id"],
    )
    op.create_unique_constraint(
        "uq_publications_compilation_channel",
        "publications",
        ["compilation_id", "youtube_connection_id"],
    )
    op.create_check_constraint(
        "ck_publications_exactly_one_source",
        "publications",
        "(production_id IS NOT NULL AND compilation_id IS NULL) OR "
        "(production_id IS NULL AND compilation_id IS NOT NULL)",
    )


def downgrade() -> None:
    op.execute("DELETE FROM publications WHERE compilation_id IS NOT NULL")
    op.drop_constraint(
        "ck_publications_exactly_one_source",
        "publications",
        type_="check",
    )
    op.drop_constraint(
        "uq_publications_compilation_channel",
        "publications",
        type_="unique",
    )
    op.drop_index("ix_publications_compilation_id", table_name="publications")
    op.drop_constraint(
        "fk_publications_compilation_id_compilations",
        "publications",
        type_="foreignkey",
    )
    op.drop_column("publications", "compilation_id")
    op.alter_column("publications", "production_id", existing_type=sa.Uuid(), nullable=False)
