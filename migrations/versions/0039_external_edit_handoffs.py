"""add external edit handoffs

Revision ID: 0039_external_edit_handoffs
Revises: 0038_command_action_proposals
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0039_external_edit_handoffs"
down_revision: str | None = "0038_command_action_proposals"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "external_edit_handoffs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("source_type", sa.String(length=32), nullable=False),
        sa.Column("source_id", sa.Uuid(), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("package_manifest_key", sa.Text(), nullable=False),
        sa.Column("output_key", sa.Text(), nullable=True),
        sa.Column("external_project_id", sa.String(length=255), nullable=True),
        sa.Column("handoff_metadata", sa.JSON(), nullable=False),
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
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "provider IN ('invideo')",
            name="ck_external_edit_provider",
        ),
        sa.CheckConstraint(
            "source_type IN ('production', 'short_episode')",
            name="ck_external_edit_source_type",
        ),
        sa.CheckConstraint(
            "status IN ('prepared', 'output_imported', 'adopted', 'cancelled', 'failed')",
            name="ck_external_edit_status",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "provider",
            "source_type",
            "source_id",
            "generation",
            name="uq_external_edit_handoff_generation",
        ),
    )
    for column in ("provider", "source_type", "source_id", "status"):
        op.create_index(
            f"ix_external_edit_handoffs_{column}",
            "external_edit_handoffs",
            [column],
        )


def downgrade() -> None:
    for column in reversed(("provider", "source_type", "source_id", "status")):
        op.drop_index(
            f"ix_external_edit_handoffs_{column}",
            table_name="external_edit_handoffs",
        )
    op.drop_table("external_edit_handoffs")
