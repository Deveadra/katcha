"""add durable render attempts

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
        sa.Column("production_id", sa.Uuid(), nullable=True),
        sa.Column("short_episode_id", sa.Uuid(), nullable=True),
        sa.Column("retry_of_id", sa.Uuid(), nullable=True),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("workflow_id", sa.String(length=255), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("requested_by", sa.String(length=128), nullable=False),
        sa.Column("manifest_version", sa.String(length=64), nullable=True),
        sa.Column("manifest_sha256", sa.String(length=64), nullable=True),
        sa.Column("manifest_snapshot", sa.JSON(), nullable=False),
        sa.Column("output_key", sa.Text(), nullable=True),
        sa.Column("pre_qc", sa.JSON(), nullable=False),
        sa.Column("post_qc", sa.JSON(), nullable=False),
        sa.Column("failure_kind", sa.String(length=64), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("attempt_metadata", sa.JSON(), nullable=False),
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
            """
            (production_id IS NOT NULL AND short_episode_id IS NULL)
            OR
            (production_id IS NULL AND short_episode_id IS NOT NULL)
            """,
            name="ck_render_attempt_exactly_one_source",
        ),
        sa.CheckConstraint(
            "attempt_number > 0",
            name="ck_render_attempt_number_positive",
        ),
        sa.ForeignKeyConstraint(["production_id"], ["productions.id"]),
        sa.ForeignKeyConstraint(["short_episode_id"], ["short_episodes.id"]),
        sa.ForeignKeyConstraint(["retry_of_id"], ["render_attempts.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("workflow_id"),
        sa.UniqueConstraint("production_id", "attempt_number"),
        sa.UniqueConstraint("short_episode_id", "attempt_number"),
    )
    for column in (
        "production_id",
        "short_episode_id",
        "retry_of_id",
        "workflow_id",
        "status",
        "created_at",
    ):
        op.create_index(
            f"ix_render_attempts_{column}",
            "render_attempts",
            [column],
        )


def downgrade() -> None:
    op.drop_table("render_attempts")
