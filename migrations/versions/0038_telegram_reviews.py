"""add Telegram operator review state

Revision ID: 0038_telegram_reviews
Revises: 0037_clip_lifecycle
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0038_telegram_reviews"
down_revision: str | None = "0037_clip_lifecycle"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "telegram_review_sessions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("source_kind", sa.String(length=32), nullable=False),
        sa.Column("source_id", sa.Uuid(), nullable=False),
        sa.Column("channel_profile_id", sa.Uuid(), nullable=True),
        sa.Column("callback_token", sa.String(length=24), nullable=False),
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("message_id", sa.BigInteger(), nullable=True),
        sa.Column("telegram_file_id", sa.Text(), nullable=True),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column("feedback_prompt_message_id", sa.BigInteger(), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("session_metadata", sa.JSON(), nullable=False),
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
        sa.CheckConstraint(
            "source_kind IN ('short_episode', 'production', 'compilation')",
            name="ck_telegram_review_source_kind",
        ),
        sa.CheckConstraint(
            "state IN ('queued', 'sent', 'awaiting_edit_feedback', "
            "'backlogged', 'approved', 'rejected', 'regenerating', "
            "'superseded', 'failed')",
            name="ck_telegram_review_state",
        ),
        sa.ForeignKeyConstraint(["channel_profile_id"], ["channel_profiles.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("callback_token"),
    )
    for column in (
        "source_kind",
        "source_id",
        "channel_profile_id",
        "callback_token",
        "chat_id",
        "state",
    ):
        op.create_index(
            f"ix_telegram_review_sessions_{column}",
            "telegram_review_sessions",
            [column],
        )

    op.create_table(
        "telegram_bot_cursors",
        sa.Column("key", sa.String(length=64), nullable=False),
        sa.Column("last_update_id", sa.BigInteger(), nullable=False),
        sa.Column("cursor_metadata", sa.JSON(), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("key"),
    )


def downgrade() -> None:
    op.drop_table("telegram_bot_cursors")
    for column in reversed(
        (
            "source_kind",
            "source_id",
            "channel_profile_id",
            "callback_token",
            "chat_id",
            "state",
        )
    ):
        op.drop_index(
            f"ix_telegram_review_sessions_{column}",
            table_name="telegram_review_sessions",
        )
    op.drop_table("telegram_review_sessions")
