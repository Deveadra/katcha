"""add clip lifecycle and retention policies

Revision ID: 0037_clip_lifecycle
Revises: 0036_ingestion_sources
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0037_clip_lifecycle"
down_revision: str | None = "0036_ingestion_sources"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "clip_lifecycle",
        sa.Column("clip_id", sa.Uuid(), nullable=False),
        sa.Column("lifecycle_state", sa.String(length=32), nullable=False),
        sa.Column("archive_key", sa.Text(), nullable=True),
        sa.Column("tags", sa.JSON(), nullable=False),
        sa.Column("library_metadata", sa.JSON(), nullable=False),
        sa.Column("search_document", sa.Text(), nullable=False),
        sa.Column("embedding_metadata", sa.JSON(), nullable=False),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("purged_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "lifecycle_state IN ('hot', 'archived', 'purged')",
            name="ck_clip_lifecycle_state",
        ),
        sa.ForeignKeyConstraint(["clip_id"], ["clips.id"]),
        sa.PrimaryKeyConstraint("clip_id"),
    )
    for column in ("lifecycle_state", "archived_at", "purged_at"):
        op.create_index(
            f"ix_clip_lifecycle_{column}",
            "clip_lifecycle",
            [column],
        )

    op.create_table(
        "clip_retention_policies",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("channel_profile_id", sa.Uuid(), nullable=False),
        sa.Column("retention_mode", sa.String(length=32), nullable=False),
        sa.Column("auto_archive", sa.Boolean(), nullable=False),
        sa.Column("auto_purge", sa.Boolean(), nullable=False),
        sa.Column("auto_remove_duplicates", sa.Boolean(), nullable=False),
        sa.Column("archive_after_days", sa.Integer(), nullable=True),
        sa.Column("purge_after_days", sa.Integer(), nullable=True),
        sa.Column("failed_purge_after_days", sa.Integer(), nullable=True),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("confirmed_by", sa.String(length=128), nullable=True),
        sa.Column("policy_metadata", sa.JSON(), nullable=False),
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
            "retention_mode IN ('indefinite', 'managed')",
            name="ck_clip_retention_mode",
        ),
        sa.CheckConstraint(
            "archive_after_days IS NULL OR archive_after_days > 0",
            name="ck_clip_retention_archive_days",
        ),
        sa.CheckConstraint(
            "purge_after_days IS NULL OR purge_after_days > 0",
            name="ck_clip_retention_purge_days",
        ),
        sa.CheckConstraint(
            "failed_purge_after_days IS NULL OR failed_purge_after_days > 0",
            name="ck_clip_retention_failed_days",
        ),
        sa.ForeignKeyConstraint(["channel_profile_id"], ["channel_profiles.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("channel_profile_id", name="uq_clip_retention_channel"),
    )
    for column in ("channel_profile_id", "retention_mode"):
        op.create_index(
            f"ix_clip_retention_policies_{column}",
            "clip_retention_policies",
            [column],
        )


def downgrade() -> None:
    for column in reversed(("channel_profile_id", "retention_mode")):
        op.drop_index(
            f"ix_clip_retention_policies_{column}",
            table_name="clip_retention_policies",
        )
    op.drop_table("clip_retention_policies")

    for column in reversed(("lifecycle_state", "archived_at", "purged_at")):
        op.drop_index(f"ix_clip_lifecycle_{column}", table_name="clip_lifecycle")
    op.drop_table("clip_lifecycle")
