"""add durable trend review queue

Revision ID: 0016_trend_review_queue
Revises: 0015_trend_watches
Create Date: 2026-09-16
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0016_trend_review_queue"
down_revision: str | None = "0015_trend_watches"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "trend_review_queue_items",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("topic_watch_id", sa.Uuid(), nullable=False),
        sa.Column("discovery_candidate_id", sa.Uuid(), nullable=False),
        sa.Column("trend_score_id", sa.Uuid(), nullable=False),
        sa.Column("queue_key", sa.String(length=160), nullable=False),
        sa.Column("rank", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("queue_metadata", sa.JSON(), nullable=False),
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
        sa.CheckConstraint("rank > 0", name="ck_trend_review_queue_rank_positive"),
        sa.ForeignKeyConstraint(
            ["topic_watch_id"],
            ["topic_watch_versions.id"],
        ),
        sa.ForeignKeyConstraint(
            ["discovery_candidate_id"],
            ["discovery_candidates.id"],
        ),
        sa.ForeignKeyConstraint(
            ["trend_score_id"],
            ["candidate_trend_scores.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "topic_watch_id",
            "queue_key",
            "discovery_candidate_id",
        ),
    )
    op.create_index(
        "ix_trend_review_queue_items_topic_watch_id",
        "trend_review_queue_items",
        ["topic_watch_id"],
    )
    op.create_index(
        "ix_trend_review_queue_items_discovery_candidate_id",
        "trend_review_queue_items",
        ["discovery_candidate_id"],
    )
    op.create_index(
        "ix_trend_review_queue_items_trend_score_id",
        "trend_review_queue_items",
        ["trend_score_id"],
    )
    op.create_index(
        "ix_trend_review_queue_items_queue_key",
        "trend_review_queue_items",
        ["queue_key"],
    )
    op.create_index(
        "ix_trend_review_queue_items_status",
        "trend_review_queue_items",
        ["status"],
    )
    op.create_index(
        "ix_trend_review_queue_items_created_at",
        "trend_review_queue_items",
        ["created_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_trend_review_queue_items_created_at",
        table_name="trend_review_queue_items",
    )
    op.drop_index(
        "ix_trend_review_queue_items_status",
        table_name="trend_review_queue_items",
    )
    op.drop_index(
        "ix_trend_review_queue_items_queue_key",
        table_name="trend_review_queue_items",
    )
    op.drop_index(
        "ix_trend_review_queue_items_trend_score_id",
        table_name="trend_review_queue_items",
    )
    op.drop_index(
        "ix_trend_review_queue_items_discovery_candidate_id",
        table_name="trend_review_queue_items",
    )
    op.drop_index(
        "ix_trend_review_queue_items_topic_watch_id",
        table_name="trend_review_queue_items",
    )
    op.drop_table("trend_review_queue_items")
