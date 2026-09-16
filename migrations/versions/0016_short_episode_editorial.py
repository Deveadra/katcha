"""add short episode editorial and voice lineage

Revision ID: 0016_short_episode_editorial
Revises: 0015_trend_intelligence
Create Date: 2026-09-16
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0016_short_episode_editorial"
down_revision: str | None = "0015_trend_intelligence"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "short_episodes",
        sa.Column(
            "prompt_version",
            sa.String(length=32),
            nullable=False,
            server_default="ranked-episode-v1",
        ),
    )
    op.add_column(
        "short_episodes",
        sa.Column("selected_script_id", sa.Uuid(), nullable=True),
    )
    op.add_column(
        "short_episodes",
        sa.Column("selected_voice_profile", sa.String(length=64), nullable=True),
    )
    op.create_index(
        "ix_short_episodes_selected_script_id",
        "short_episodes",
        ["selected_script_id"],
    )

    op.create_table(
        "short_episode_scripts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("short_episode_id", sa.Uuid(), nullable=False),
        sa.Column("candidate_index", sa.Integer(), nullable=False),
        sa.Column("style", sa.String(length=32), nullable=False),
        sa.Column("script_payload", sa.JSON(), nullable=False),
        sa.Column("narration_beats", sa.JSON(), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("model", sa.String(length=128), nullable=False),
        sa.Column("prompt_version", sa.String(length=32), nullable=False),
        sa.Column("selected", sa.Boolean(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["short_episode_id"], ["short_episodes.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("short_episode_id", "candidate_index"),
    )
    op.create_index(
        "ix_short_episode_scripts_short_episode_id",
        "short_episode_scripts",
        ["short_episode_id"],
    )
    op.create_index(
        "ix_short_episode_scripts_style",
        "short_episode_scripts",
        ["style"],
    )
    op.create_foreign_key(
        "fk_short_episodes_selected_script_id",
        "short_episodes",
        "short_episode_scripts",
        ["selected_script_id"],
        ["id"],
    )

    op.create_table(
        "short_episode_assets",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("short_episode_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(length=64), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("storage_key", sa.Text(), nullable=False),
        sa.Column("content_type", sa.String(length=128), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=True),
        sa.Column("model", sa.String(length=128), nullable=True),
        sa.Column("asset_metadata", sa.JSON(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["short_episode_id"], ["short_episodes.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("short_episode_id", "kind", "generation"),
    )
    op.create_index(
        "ix_short_episode_assets_short_episode_id",
        "short_episode_assets",
        ["short_episode_id"],
    )
    op.create_index(
        "ix_short_episode_assets_kind",
        "short_episode_assets",
        ["kind"],
    )

    op.create_table(
        "short_episode_reviews",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("short_episode_id", sa.Uuid(), nullable=False),
        sa.Column("decision", sa.String(length=32), nullable=False),
        sa.Column("actor", sa.String(length=128), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("review_metadata", sa.JSON(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["short_episode_id"], ["short_episodes.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_short_episode_reviews_short_episode_id",
        "short_episode_reviews",
        ["short_episode_id"],
    )
    op.create_index(
        "ix_short_episode_reviews_decision",
        "short_episode_reviews",
        ["decision"],
    )


def downgrade() -> None:
    op.drop_index("ix_short_episode_reviews_decision", table_name="short_episode_reviews")
    op.drop_index(
        "ix_short_episode_reviews_short_episode_id",
        table_name="short_episode_reviews",
    )
    op.drop_table("short_episode_reviews")
    op.drop_index("ix_short_episode_assets_kind", table_name="short_episode_assets")
    op.drop_index(
        "ix_short_episode_assets_short_episode_id",
        table_name="short_episode_assets",
    )
    op.drop_table("short_episode_assets")
    op.drop_constraint(
        "fk_short_episodes_selected_script_id",
        "short_episodes",
        type_="foreignkey",
    )
    op.drop_index("ix_short_episode_scripts_style", table_name="short_episode_scripts")
    op.drop_index(
        "ix_short_episode_scripts_short_episode_id",
        table_name="short_episode_scripts",
    )
    op.drop_table("short_episode_scripts")
    op.drop_index("ix_short_episodes_selected_script_id", table_name="short_episodes")
    op.drop_column("short_episodes", "selected_voice_profile")
    op.drop_column("short_episodes", "selected_script_id")
    op.drop_column("short_episodes", "prompt_version")
