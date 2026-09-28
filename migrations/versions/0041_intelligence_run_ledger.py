"""add durable channel intelligence run ledger

Revision ID: 0041_intelligence_run_ledger
Revises: 0040_command_conversations
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0041_intelligence_run_ledger"
down_revision: str | None = "0040_command_conversations"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "channel_intelligence_runs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("channel_profile_id", sa.Uuid(), nullable=False),
        sa.Column("run_key", sa.String(length=128), nullable=False),
        sa.Column("workflow_id", sa.String(length=255), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("stage", sa.String(length=64), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("result_summary", sa.JSON(), nullable=False),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
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
            "run_key",
            name="uq_channel_intelligence_runs_channel_run_key",
        ),
    )
    for column in (
        "channel_profile_id",
        "run_key",
        "workflow_id",
        "status",
    ):
        op.create_index(
            f"ix_channel_intelligence_runs_{column}",
            "channel_intelligence_runs",
            [column],
        )


def downgrade() -> None:
    for column in reversed(
        (
            "channel_profile_id",
            "run_key",
            "workflow_id",
            "status",
        )
    ):
        op.drop_index(
            f"ix_channel_intelligence_runs_{column}",
            table_name="channel_intelligence_runs",
        )
    op.drop_table("channel_intelligence_runs")
