"""add channel brand versions and production lineage

Revision ID: 0011_channel_brand_versions
Revises: 0010_budget_reservations
Create Date: 2026-09-16
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0011_channel_brand_versions"
down_revision: str | None = "0010_budget_reservations"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "channel_brand_versions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("channel_profile_id", sa.Uuid(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("brand_key", sa.String(length=64), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("contract", sa.JSON(), nullable=False),
        sa.Column("brand_metadata", sa.JSON(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["channel_profile_id"], ["channel_profiles.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("channel_profile_id", "version"),
    )
    op.create_index(
        "ix_channel_brand_versions_channel_profile_id",
        "channel_brand_versions",
        ["channel_profile_id"],
    )
    op.create_index(
        "ix_channel_brand_versions_brand_key",
        "channel_brand_versions",
        ["brand_key"],
    )
    op.create_index(
        "ix_channel_brand_versions_is_active",
        "channel_brand_versions",
        ["is_active"],
    )

    op.add_column(
        "productions",
        sa.Column("brand_key", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "productions",
        sa.Column("brand_version", sa.Integer(), nullable=True),
    )
    op.add_column(
        "productions",
        sa.Column("brand_snapshot", sa.JSON(), nullable=True),
    )
    op.create_index("ix_productions_brand_key", "productions", ["brand_key"])


def downgrade() -> None:
    op.drop_index("ix_productions_brand_key", table_name="productions")
    op.drop_column("productions", "brand_snapshot")
    op.drop_column("productions", "brand_version")
    op.drop_column("productions", "brand_key")

    op.drop_index("ix_channel_brand_versions_is_active", table_name="channel_brand_versions")
    op.drop_index("ix_channel_brand_versions_brand_key", table_name="channel_brand_versions")
    op.drop_index(
        "ix_channel_brand_versions_channel_profile_id",
        table_name="channel_brand_versions",
    )
    op.drop_table("channel_brand_versions")
