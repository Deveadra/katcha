"""Durable goal receipts and frozen execution steps."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0046_command_goals"
down_revision: str | None = "0045_merge_codex_interim_heads"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "command_goals",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("command_id", sa.Uuid(), nullable=False),
        sa.Column(
            "channel_profile_id", sa.Uuid(), sa.ForeignKey("channel_profiles.id"), nullable=False
        ),
        sa.Column("thread_id", sa.Uuid(), sa.ForeignKey("command_threads.id"), nullable=False),
        sa.Column("actor", sa.String(128), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("request", sa.JSON(), nullable=False),
        sa.Column("authority", sa.JSON(), nullable=False),
        sa.Column("authorization", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(40), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("step_count", sa.Integer(), nullable=False),
        sa.Column("observations", sa.JSON(), nullable=False),
        sa.Column("result", sa.JSON(), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("deadline", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint("actor", "command_id"),
    )
    for column in ("command_id", "status"):
        op.create_index(f"ix_command_goals_{column}", "command_goals", [column])
    op.create_table(
        "command_goal_steps",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("goal_id", sa.Uuid(), sa.ForeignKey("command_goals.id"), nullable=False),
        sa.Column("number", sa.Integer(), nullable=False),
        sa.Column("decision", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(40), nullable=False),
        sa.Column(
            "proposal_id", sa.Uuid(), sa.ForeignKey("command_action_proposals.id"), nullable=True
        ),
        sa.Column("result", sa.JSON(), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint("goal_id", "number"),
    )
    op.create_index("ix_command_goal_steps_goal_id", "command_goal_steps", ["goal_id"])


def downgrade() -> None:
    op.drop_table("command_goal_steps")
    op.drop_table("command_goals")
