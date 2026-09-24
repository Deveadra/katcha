"""add durable render attempt ledger

Revision ID: 0026_render_attempts
Revises: 0025_trend_activation_performance
Create Date: 2026-09-24
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0026_render_attempts"
down_revision: str | None = "0025_trend_activation_performance"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "render_attempts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("attempt_key", sa.String(length=255), nullable=False),
        sa.Column("production_id", sa.Uuid(), nullable=True),
        sa.Column("short_episode_id", sa.Uuid(), nullable=True),
        sa.Column("channel_profile_id", sa.Uuid(), nullable=True),
        sa.Column("parent_attempt_id", sa.Uuid(), nullable=True),
        sa.Column("source_generation", sa.Integer(), nullable=False),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("stage", sa.String(length=64), nullable=False),
        sa.Column("output_key", sa.Text(), nullable=False),
        sa.Column("manifest_version", sa.String(length=64), nullable=False),
        sa.Column("verification", sa.JSON(), nullable=False),
        sa.Column("failure_count", sa.Integer(), nullable=False),
        sa.Column("last_failure_class", sa.String(length=128), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "(production_id IS NOT NULL AND short_episode_id IS NULL) OR "
            "(production_id IS NULL AND short_episode_id IS NOT NULL)",
            name="ck_render_attempts_exactly_one_source",
        ),
        sa.CheckConstraint(
            "attempt_number > 0",
            name="ck_render_attempts_attempt_number_positive",
        ),
        sa.CheckConstraint(
            "source_generation > 0",
            name="ck_render_attempts_source_generation_positive",
        ),
        sa.CheckConstraint(
            "failure_count >= 0",
            name="ck_render_attempts_failure_count_nonnegative",
        ),
        sa.ForeignKeyConstraint(["channel_profile_id"], ["channel_profiles.id"]),
        sa.ForeignKeyConstraint(["parent_attempt_id"], ["render_attempts.id"]),
        sa.ForeignKeyConstraint(["production_id"], ["productions.id"]),
        sa.ForeignKeyConstraint(["short_episode_id"], ["short_episodes.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("attempt_key"),
        sa.UniqueConstraint(
            "production_id",
            "attempt_number",
            name="uq_render_attempts_production_attempt",
        ),
        sa.UniqueConstraint(
            "short_episode_id",
            "attempt_number",
            name="uq_render_attempts_short_episode_attempt",
        ),
    )
    op.create_index(
        op.f("ix_render_attempts_attempt_key"),
        "render_attempts",
        ["attempt_key"],
        unique=True,
    )
    op.create_index(
        op.f("ix_render_attempts_channel_profile_id"),
        "render_attempts",
        ["channel_profile_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_render_attempts_parent_attempt_id"),
        "render_attempts",
        ["parent_attempt_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_render_attempts_production_id"),
        "render_attempts",
        ["production_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_render_attempts_short_episode_id"),
        "render_attempts",
        ["short_episode_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_render_attempts_status"),
        "render_attempts",
        ["status"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_render_attempts_status"), table_name="render_attempts")
    op.drop_index(
        op.f("ix_render_attempts_short_episode_id"),
        table_name="render_attempts",
    )
    op.drop_index(
        op.f("ix_render_attempts_production_id"),
        table_name="render_attempts",
    )
    op.drop_index(
        op.f("ix_render_attempts_parent_attempt_id"),
        table_name="render_attempts",
    )
    op.drop_index(
        op.f("ix_render_attempts_channel_profile_id"),
        table_name="render_attempts",
    )
    op.drop_index(op.f("ix_render_attempts_attempt_key"), table_name="render_attempts")
    op.drop_table("render_attempts")
