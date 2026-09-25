"""support ranked short episode brand previews

Revision ID: 0035_ranked_brand_previews
Revises: 0034_brand_preview_renders
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0035_ranked_brand_previews"
down_revision: str | None = "0034_brand_preview_renders"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column(
        "brand_preview_renders",
        "production_id",
        existing_type=sa.Uuid(),
        nullable=True,
    )
    op.add_column(
        "brand_preview_renders",
        sa.Column(
            "short_episode_id",
            sa.Uuid(),
            sa.ForeignKey("short_episodes.id"),
            nullable=True,
        ),
    )
    op.create_index(
        "ix_brand_preview_renders_short_episode_id",
        "brand_preview_renders",
        ["short_episode_id"],
    )
    op.create_check_constraint(
        "ck_brand_preview_exactly_one_source",
        "brand_preview_renders",
        "(production_id IS NOT NULL AND short_episode_id IS NULL) OR "
        "(production_id IS NULL AND short_episode_id IS NOT NULL)",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_brand_preview_exactly_one_source",
        "brand_preview_renders",
        type_="check",
    )
    op.drop_index(
        "ix_brand_preview_renders_short_episode_id",
        table_name="brand_preview_renders",
    )
    op.execute("DELETE FROM brand_preview_renders WHERE production_id IS NULL")
    op.drop_column("brand_preview_renders", "short_episode_id")
    op.alter_column(
        "brand_preview_renders",
        "production_id",
        existing_type=sa.Uuid(),
        nullable=False,
    )
