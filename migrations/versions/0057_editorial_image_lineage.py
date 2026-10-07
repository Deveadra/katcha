"""add derived source lineage to editorial images

Revision ID: 0057_editorial_image_lineage
Revises: 0056_editorial_publications
Create Date: 2026-10-07
"""

import sqlalchemy as sa
from alembic import op

revision = "0057_editorial_image_lineage"
down_revision = "0056_editorial_publications"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "editorial_images",
        sa.Column(
            "source_metadata",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
    )


def downgrade() -> None:
    op.drop_column("editorial_images", "source_metadata")
