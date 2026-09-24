"""persist channel edit blueprints and production lineage

Revision ID: 0023_channel_edit_blueprints
Revises: 0022_discovery_poll_quota
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0023_channel_edit_blueprints"
down_revision: str | None = "0022_discovery_poll_quota"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "channel_edit_blueprint_versions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("channel_profile_id", sa.Uuid(), nullable=False),
        sa.Column("blueprint_key", sa.String(length=96), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("contract_version", sa.String(length=32), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("is_default", sa.Boolean(), nullable=False),
        sa.Column("contract", sa.JSON(), nullable=False),
        sa.Column("blueprint_metadata", sa.JSON(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "version > 0",
            name="ck_channel_edit_blueprint_version_positive",
        ),
        sa.ForeignKeyConstraint(["channel_profile_id"], ["channel_profiles.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("channel_profile_id", "blueprint_key", "version"),
    )
    op.create_index(
        "ix_channel_edit_blueprint_versions_channel_profile_id",
        "channel_edit_blueprint_versions",
        ["channel_profile_id"],
    )
    op.create_index(
        "ix_channel_edit_blueprint_versions_blueprint_key",
        "channel_edit_blueprint_versions",
        ["blueprint_key"],
    )
    op.create_index(
        "ix_channel_edit_blueprint_versions_is_active",
        "channel_edit_blueprint_versions",
        ["is_active"],
    )
    op.create_index(
        "ix_channel_edit_blueprint_versions_is_default",
        "channel_edit_blueprint_versions",
        ["is_default"],
    )
    op.create_index(
        "uq_channel_edit_blueprint_active_family",
        "channel_edit_blueprint_versions",
        ["channel_profile_id", "blueprint_key"],
        unique=True,
        postgresql_where=sa.text("is_active"),
    )
    op.create_index(
        "uq_channel_edit_blueprint_default",
        "channel_edit_blueprint_versions",
        ["channel_profile_id"],
        unique=True,
        postgresql_where=sa.text("is_default"),
    )

    for table in ("productions", "short_episodes"):
        op.add_column(
            table,
            sa.Column("edit_blueprint_key", sa.String(length=96), nullable=True),
        )
        op.add_column(
            table,
            sa.Column("edit_blueprint_version", sa.Integer(), nullable=True),
        )
        op.add_column(
            table,
            sa.Column("edit_blueprint_snapshot", sa.JSON(), nullable=True),
        )
        op.create_index(
            f"ix_{table}_edit_blueprint_key",
            table,
            ["edit_blueprint_key"],
        )


def downgrade() -> None:
    for table in ("short_episodes", "productions"):
        op.drop_index(f"ix_{table}_edit_blueprint_key", table_name=table)
        op.drop_column(table, "edit_blueprint_snapshot")
        op.drop_column(table, "edit_blueprint_version")
        op.drop_column(table, "edit_blueprint_key")

    op.drop_index(
        "uq_channel_edit_blueprint_default",
        table_name="channel_edit_blueprint_versions",
    )
    op.drop_index(
        "uq_channel_edit_blueprint_active_family",
        table_name="channel_edit_blueprint_versions",
    )
    op.drop_index(
        "ix_channel_edit_blueprint_versions_is_default",
        table_name="channel_edit_blueprint_versions",
    )
    op.drop_index(
        "ix_channel_edit_blueprint_versions_is_active",
        table_name="channel_edit_blueprint_versions",
    )
    op.drop_index(
        "ix_channel_edit_blueprint_versions_blueprint_key",
        table_name="channel_edit_blueprint_versions",
    )
    op.drop_index(
        "ix_channel_edit_blueprint_versions_channel_profile_id",
        table_name="channel_edit_blueprint_versions",
    )
    op.drop_table("channel_edit_blueprint_versions")
