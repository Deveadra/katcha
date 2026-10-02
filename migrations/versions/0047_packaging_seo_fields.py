"""Add SEO fields to packaging variants."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0047_packaging_seo_fields"
down_revision: str | None = "0046_command_goals"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "publication_packaging_variants",
        sa.Column("tags", sa.JSON(), nullable=True),
    )
    op.add_column(
        "publication_packaging_variants",
        sa.Column("hashtags", sa.JSON(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("publication_packaging_variants", "hashtags")
    op.drop_column("publication_packaging_variants", "tags")
