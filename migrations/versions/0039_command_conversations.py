"""add durable command center conversations

Revision ID: 0039_command_conversations
Revises: 0038_command_action_proposals
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0039_command_conversations"
down_revision: str | None = "0038_command_action_proposals"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "command_threads",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("channel_profile_id", sa.Uuid(), nullable=False),
        sa.Column("title", sa.String(length=160), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("created_by", sa.String(length=128), nullable=False),
        sa.Column("thread_metadata", sa.JSON(), nullable=False),
        sa.Column(
            "last_activity_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
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
    )
    for column in ("channel_profile_id", "status", "last_activity_at"):
        op.create_index(
            f"ix_command_threads_{column}",
            "command_threads",
            [column],
        )

    op.create_table(
        "command_turns",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("thread_id", sa.Uuid(), nullable=False),
        sa.Column("channel_profile_id", sa.Uuid(), nullable=False),
        sa.Column("sequence_number", sa.Integer(), nullable=False),
        sa.Column("role", sa.String(length=32), nullable=False),
        sa.Column("request_id", sa.Uuid(), nullable=True),
        sa.Column("intent", sa.String(length=64), nullable=True),
        sa.Column("narrator", sa.String(length=160), nullable=True),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("evidence", sa.JSON(), nullable=False),
        sa.Column("turn_context", sa.JSON(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["channel_profile_id"], ["channel_profiles.id"]),
        sa.ForeignKeyConstraint(["thread_id"], ["command_threads.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "thread_id",
            "sequence_number",
            name="uq_command_turns_thread_sequence",
        ),
    )
    for column in (
        "thread_id",
        "channel_profile_id",
        "role",
        "request_id",
        "created_at",
    ):
        op.create_index(
            f"ix_command_turns_{column}",
            "command_turns",
            [column],
        )

    op.add_column(
        "command_action_proposals",
        sa.Column("thread_id", sa.Uuid(), nullable=True),
    )
    op.add_column(
        "command_action_proposals",
        sa.Column("source_turn_id", sa.Uuid(), nullable=True),
    )
    op.create_foreign_key(
        "fk_command_action_proposals_thread_id",
        "command_action_proposals",
        "command_threads",
        ["thread_id"],
        ["id"],
    )
    op.create_foreign_key(
        "fk_command_action_proposals_source_turn_id",
        "command_action_proposals",
        "command_turns",
        ["source_turn_id"],
        ["id"],
    )
    op.create_index(
        "ix_command_action_proposals_thread_id",
        "command_action_proposals",
        ["thread_id"],
    )
    op.create_index(
        "ix_command_action_proposals_source_turn_id",
        "command_action_proposals",
        ["source_turn_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_command_action_proposals_source_turn_id",
        table_name="command_action_proposals",
    )
    op.drop_index(
        "ix_command_action_proposals_thread_id",
        table_name="command_action_proposals",
    )
    op.drop_constraint(
        "fk_command_action_proposals_source_turn_id",
        "command_action_proposals",
        type_="foreignkey",
    )
    op.drop_constraint(
        "fk_command_action_proposals_thread_id",
        "command_action_proposals",
        type_="foreignkey",
    )
    op.drop_column("command_action_proposals", "source_turn_id")
    op.drop_column("command_action_proposals", "thread_id")

    for column in reversed(
        ("thread_id", "channel_profile_id", "role", "request_id", "created_at")
    ):
        op.drop_index(
            f"ix_command_turns_{column}",
            table_name="command_turns",
        )
    op.drop_table("command_turns")

    for column in reversed(("channel_profile_id", "status", "last_activity_at")):
        op.drop_index(
            f"ix_command_threads_{column}",
            table_name="command_threads",
        )
    op.drop_table("command_threads")
