"""add publication packaging variants and activation ledger

Revision ID: 0028_publication_packaging
Revises: 0027_edit_blueprint_performance
Create Date: 2026-09-24
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0028_publication_packaging"
down_revision: str | None = "0027_edit_blueprint_performance"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "publication_packaging_variants",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("publication_id", sa.Uuid(), nullable=False),
        sa.Column("variant_key", sa.String(length=128), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(length=100), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("thumbnail_storage_key", sa.Text(), nullable=True),
        sa.Column("thumbnail_content_type", sa.String(length=64), nullable=True),
        sa.Column("thumbnail_size_bytes", sa.BigInteger(), nullable=True),
        sa.Column("thumbnail_sha256", sa.String(length=64), nullable=True),
        sa.Column("created_by", sa.String(length=128), nullable=False),
        sa.Column("variant_metadata", sa.JSON(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "version > 0",
            name="ck_packaging_variant_version_positive",
        ),
        sa.CheckConstraint(
            "(thumbnail_storage_key IS NULL "
            "AND thumbnail_content_type IS NULL "
            "AND thumbnail_size_bytes IS NULL "
            "AND thumbnail_sha256 IS NULL) OR "
            "(thumbnail_storage_key IS NOT NULL "
            "AND thumbnail_content_type IS NOT NULL "
            "AND thumbnail_size_bytes IS NOT NULL "
            "AND thumbnail_sha256 IS NOT NULL)",
            name="ck_packaging_variant_thumbnail_metadata_complete",
        ),
        sa.CheckConstraint(
            "thumbnail_size_bytes IS NULL OR thumbnail_size_bytes > 0",
            name="ck_packaging_variant_thumbnail_size_positive",
        ),
        sa.ForeignKeyConstraint(["publication_id"], ["publications.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "publication_id",
            "variant_key",
            "version",
            name="uq_packaging_variant_publication_key_version",
        ),
    )
    op.create_index(
        "ix_publication_packaging_variants_publication_id",
        "publication_packaging_variants",
        ["publication_id"],
    )
    op.create_index(
        "ix_publication_packaging_variants_created_at",
        "publication_packaging_variants",
        ["created_at"],
    )

    op.create_table(
        "publication_packaging_activations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("publication_id", sa.Uuid(), nullable=False),
        sa.Column("variant_id", sa.Uuid(), nullable=False),
        sa.Column("activation_key", sa.String(length=160), nullable=False),
        sa.Column("workflow_id", sa.String(length=255), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("stage", sa.String(length=64), nullable=False),
        sa.Column("youtube_video_id", sa.String(length=64), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("activation_metadata", sa.JSON(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("applied_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'applied', 'failed')",
            name="ck_packaging_activation_status",
        ),
        sa.ForeignKeyConstraint(
            ["publication_id"],
            ["publications.id"],
        ),
        sa.ForeignKeyConstraint(
            ["variant_id"],
            ["publication_packaging_variants.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "publication_id",
            "activation_key",
            name="uq_packaging_activation_publication_key",
        ),
        sa.UniqueConstraint("workflow_id"),
    )
    for column in (
        "publication_id",
        "variant_id",
        "workflow_id",
        "status",
        "youtube_video_id",
        "created_at",
    ):
        op.create_index(
            f"ix_publication_packaging_activations_{column}",
            "publication_packaging_activations",
            [column],
        )


def downgrade() -> None:
    op.drop_table("publication_packaging_activations")
    op.drop_table("publication_packaging_variants")
