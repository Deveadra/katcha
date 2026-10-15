"""Add shared encrypted AI settings without altering environment credentials."""

import sqlalchemy as sa
from alembic import op

revision = "0064_application_ai_settings"
down_revision = "0063_authoritative_search_ledger"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "application_ai_settings",
        sa.Column("key", sa.String(64), primary_key=True),
        sa.Column("value", sa.Text(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("application_ai_settings")
