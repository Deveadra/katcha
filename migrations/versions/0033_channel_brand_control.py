"""enforce one active channel brand version

Revision ID: 0033_channel_brand_control
Revises: 0032_packaging_experiments
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0033_channel_brand_control"
down_revision: str | None = "0032_packaging_experiments"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "uq_channel_brand_active",
        "channel_brand_versions",
        ["channel_profile_id"],
        unique=True,
        postgresql_where=sa.text("is_active"),
        sqlite_where=sa.text("is_active = 1"),
    )


def downgrade() -> None:
    op.drop_index("uq_channel_brand_active", table_name="channel_brand_versions")
