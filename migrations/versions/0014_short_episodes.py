"""add ranked short episodes

Revision ID: 0014_short_episodes
Revises: 0013_discovery_observations
Create Date: 2026-09-16
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0014_short_episodes"
down_revision: str | None = "0013_discovery_observations"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "short_episodes",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("channel_profile_id", sa.Uuid(), nullable=False),
        sa.Column("parent_episode_id", sa.Uuid(), nullable=True),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("regenerate_from", sa.String(length=32), nullable=True),
        sa.Column("workflow_id", sa.String(length=255), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("stage", sa.String(length=64), nullable=False),
        sa.Column("premise", sa.Text(), nullable=False),
        sa.Column("format_key", sa.String(length=64), nullable=False),
        sa.Column("format_version", sa.String(length=32), nullable=False),
        sa.Column("item_count", sa.Integer(), nullable=False),
        sa.Column("format_snapshot", sa.JSON(), nullable=False),
        sa.Column("plan_snapshot", sa.JSON(), nullable=False),
        sa.Column("persona_key", sa.String(length=64), nullable=False),
        sa.Column("persona_version", sa.String(length=32), nullable=False),
        sa.Column("brand_key", sa.String(length=64), nullable=False),
        sa.Column("brand_version", sa.Integer(), nullable=False),
        sa.Column("brand_snapshot", sa.JSON(), nullable=False),
        sa.Column("estimated_cost_usd", sa.Numeric(precision=14, scale=8), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
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
            "item_count > 0",
            name="ck_short_episode_item_count_positive",
        ),
        sa.ForeignKeyConstraint(["channel_profile_id"], ["channel_profiles.id"]),
        sa.ForeignKeyConstraint(["parent_episode_id"], ["short_episodes.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("workflow_id"),
    )
    op.create_index(
        "ix_short_episodes_channel_profile_id",
        "short_episodes",
        ["channel_profile_id"],
    )
    op.create_index(
        "ix_short_episodes_parent_episode_id",
        "short_episodes",
        ["parent_episode_id"],
    )
    op.create_index("ix_short_episodes_workflow_id", "short_episodes", ["workflow_id"])
    op.create_index("ix_short_episodes_status", "short_episodes", ["status"])
    op.create_index("ix_short_episodes_format_key", "short_episodes", ["format_key"])
    op.create_index("ix_short_episodes_brand_key", "short_episodes", ["brand_key"])

    op.create_table(
        "short_episode_items",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("short_episode_id", sa.Uuid(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("clip_id", sa.Uuid(), nullable=False),
        sa.Column("role", sa.String(length=32), nullable=False),
        sa.Column("overall_score", sa.Numeric(precision=8, scale=4), nullable=False),
        sa.Column("role_score", sa.Numeric(precision=8, scale=4), nullable=False),
        sa.Column("editorial_signals", sa.JSON(), nullable=False),
        sa.Column("analysis_snapshot", sa.JSON(), nullable=False),
        sa.Column("acquisition_snapshot", sa.JSON(), nullable=False),
        sa.Column("source_snapshot", sa.JSON(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "position > 0",
            name="ck_short_episode_item_position_positive",
        ),
        sa.ForeignKeyConstraint(["short_episode_id"], ["short_episodes.id"]),
        sa.ForeignKeyConstraint(["clip_id"], ["clips.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("short_episode_id", "position"),
        sa.UniqueConstraint("short_episode_id", "clip_id"),
    )
    op.create_index(
        "ix_short_episode_items_short_episode_id",
        "short_episode_items",
        ["short_episode_id"],
    )
    op.create_index(
        "ix_short_episode_items_clip_id",
        "short_episode_items",
        ["clip_id"],
    )
    op.create_index(
        "ix_short_episode_items_role",
        "short_episode_items",
        ["role"],
    )


def downgrade() -> None:
    op.drop_index("ix_short_episode_items_role", table_name="short_episode_items")
    op.drop_index("ix_short_episode_items_clip_id", table_name="short_episode_items")
    op.drop_index(
        "ix_short_episode_items_short_episode_id",
        table_name="short_episode_items",
    )
    op.drop_table("short_episode_items")
    op.drop_index("ix_short_episodes_brand_key", table_name="short_episodes")
    op.drop_index("ix_short_episodes_format_key", table_name="short_episodes")
    op.drop_index("ix_short_episodes_status", table_name="short_episodes")
    op.drop_index("ix_short_episodes_workflow_id", table_name="short_episodes")
    op.drop_index("ix_short_episodes_parent_episode_id", table_name="short_episodes")
    op.drop_index("ix_short_episodes_channel_profile_id", table_name="short_episodes")
    op.drop_table("short_episodes")
