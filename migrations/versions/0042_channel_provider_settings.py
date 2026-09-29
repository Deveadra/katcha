"""add channel provider settings

Revision ID: 0042_channel_provider_settings
Revises: 0041_external_edit_handoffs
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0042_channel_provider_settings"
down_revision: str | None = "0041_external_edit_handoffs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "channel_provider_settings",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("channel_profile_id", sa.Uuid(), nullable=False),
        sa.Column("provider", sa.String(length=64), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("config", sa.JSON(), nullable=False),
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
        sa.ForeignKeyConstraint(["channel_profile_id"], ["channel_profiles.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "channel_profile_id",
            "provider",
            name="uq_channel_provider_setting",
        ),
    )
    op.create_index(
        "ix_channel_provider_settings_channel_profile_id",
        "channel_provider_settings",
        ["channel_profile_id"],
    )
    op.create_index(
        "ix_channel_provider_settings_provider",
        "channel_provider_settings",
        ["provider"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_channel_provider_settings_provider",
        table_name="channel_provider_settings",
    )
    op.drop_index(
        "ix_channel_provider_settings_channel_profile_id",
        table_name="channel_provider_settings",
    )
    op.drop_table("channel_provider_settings")
