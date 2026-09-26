"""add configurable ingestion source registry

Revision ID: 0036_ingestion_sources
Revises: 0035_ranked_brand_previews
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0036_ingestion_sources"
down_revision: str | None = "0035_ranked_brand_previews"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "ingestion_sources",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("channel_profile_id", sa.Uuid(), nullable=True),
        sa.Column("source_key", sa.String(length=128), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("adapter_key", sa.String(length=64), nullable=False),
        sa.Column("adapter_version", sa.String(length=64), nullable=False),
        sa.Column("platform", sa.String(length=32), nullable=False),
        sa.Column("usage_mode", sa.String(length=32), nullable=False),
        sa.Column("query_template", sa.JSON(), nullable=False),
        sa.Column("default_candidate_metadata", sa.JSON(), nullable=False),
        sa.Column("poll_interval_minutes", sa.Integer(), nullable=False),
        sa.Column("source_metadata", sa.JSON(), nullable=False),
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
            "poll_interval_minutes > 0",
            name="ck_ingestion_source_poll_interval_positive",
        ),
        sa.CheckConstraint(
            "usage_mode IN ('discovery_only', 'candidate_review', "
            "'operator_authorized', 'render_allowed', 'blocked')",
            name="ck_ingestion_source_usage_mode",
        ),
        sa.ForeignKeyConstraint(["channel_profile_id"], ["channel_profiles.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("source_key", name="uq_ingestion_source_key"),
    )
    for column in (
        "channel_profile_id",
        "source_key",
        "enabled",
        "adapter_key",
        "platform",
        "usage_mode",
        "created_at",
    ):
        op.create_index(
            f"ix_ingestion_sources_{column}",
            "ingestion_sources",
            [column],
        )


def downgrade() -> None:
    for column in reversed(
        (
            "channel_profile_id",
            "source_key",
            "enabled",
            "adapter_key",
            "platform",
            "usage_mode",
            "created_at",
        )
    ):
        op.drop_index(f"ix_ingestion_sources_{column}", table_name="ingestion_sources")
    op.drop_table("ingestion_sources")
