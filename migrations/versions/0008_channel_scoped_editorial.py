"""scope editorial outputs to channel profiles

Revision ID: 0008_channel_scoped_editorial
Revises: 0007_self_sustaining_intelligence
Create Date: 2026-09-15
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008_channel_scoped_editorial"
down_revision: str | None = "0007_self_sustaining_intelligence"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("productions", sa.Column("channel_profile_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        "fk_productions_channel_profile_id",
        "productions",
        "channel_profiles",
        ["channel_profile_id"],
        ["id"],
    )
    op.create_index(
        "ix_productions_channel_profile_id",
        "productions",
        ["channel_profile_id"],
    )

    op.add_column("compilations", sa.Column("channel_profile_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        "fk_compilations_channel_profile_id",
        "compilations",
        "channel_profiles",
        ["channel_profile_id"],
        ["id"],
    )
    op.create_index(
        "ix_compilations_channel_profile_id",
        "compilations",
        ["channel_profile_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_compilations_channel_profile_id", table_name="compilations")
    op.drop_constraint(
        "fk_compilations_channel_profile_id",
        "compilations",
        type_="foreignkey",
    )
    op.drop_column("compilations", "channel_profile_id")

    op.drop_index("ix_productions_channel_profile_id", table_name="productions")
    op.drop_constraint(
        "fk_productions_channel_profile_id",
        "productions",
        type_="foreignkey",
    )
    op.drop_column("productions", "channel_profile_id")
