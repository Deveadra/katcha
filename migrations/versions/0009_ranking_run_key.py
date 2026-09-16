"""make ranking refresh idempotent

Revision ID: 0009_ranking_run_key
Revises: 0008_channel_scoped_editorial
Create Date: 2026-09-15
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009_ranking_run_key"
down_revision: str | None = "0008_channel_scoped_editorial"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "ranking_snapshots",
        sa.Column("run_key", sa.String(length=128), nullable=False),
    )
    op.create_index(
        "ix_ranking_snapshots_run_key",
        "ranking_snapshots",
        ["run_key"],
    )
    op.create_unique_constraint(
        "uq_ranking_snapshots_channel_run_key",
        "ranking_snapshots",
        ["channel_profile_id", "run_key"],
    )


def downgrade() -> None:
    op.drop_constraint(
        "uq_ranking_snapshots_channel_run_key",
        "ranking_snapshots",
        type_="unique",
    )
    op.drop_index("ix_ranking_snapshots_run_key", table_name="ranking_snapshots")
    op.drop_column("ranking_snapshots", "run_key")
