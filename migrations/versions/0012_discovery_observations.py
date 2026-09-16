"""preserve repeated discovery observations

Revision ID: 0012_discovery_observations
Revises: 0011_discovery_rights
Create Date: 2026-09-16
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0012_discovery_observations"
down_revision: str | None = "0011_discovery_rights"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "discovery_observations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("discovery_run_id", sa.Uuid(), nullable=False),
        sa.Column("discovery_candidate_id", sa.Uuid(), nullable=False),
        sa.Column("external_id", sa.String(length=255), nullable=True),
        sa.Column("observation_metadata", sa.JSON(), nullable=False),
        sa.Column(
            "observed_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["discovery_run_id"], ["discovery_runs.id"]),
        sa.ForeignKeyConstraint(
            ["discovery_candidate_id"],
            ["discovery_candidates.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("discovery_run_id", "discovery_candidate_id"),
    )
    op.create_index(
        "ix_discovery_observations_discovery_run_id",
        "discovery_observations",
        ["discovery_run_id"],
    )
    op.create_index(
        "ix_discovery_observations_discovery_candidate_id",
        "discovery_observations",
        ["discovery_candidate_id"],
    )
    op.create_index(
        "ix_discovery_observations_observed_at",
        "discovery_observations",
        ["observed_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_discovery_observations_observed_at",
        table_name="discovery_observations",
    )
    op.drop_index(
        "ix_discovery_observations_discovery_candidate_id",
        table_name="discovery_observations",
    )
    op.drop_index(
        "ix_discovery_observations_discovery_run_id",
        table_name="discovery_observations",
    )
    op.drop_table("discovery_observations")
