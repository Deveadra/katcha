"""add packaging candidate generation ledger

Revision ID: 0031_packaging_candidate_generation
Revises: 0030_packaging_intelligence
Create Date: 2026-09-24
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0031_packaging_candidate_generation"
down_revision: str | None = "0030_packaging_intelligence"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "packaging_candidate_generations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("publication_id", sa.Uuid(), nullable=False),
        sa.Column("generation_key", sa.String(length=160), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("stage", sa.String(length=64), nullable=False),
        sa.Column("prompt_version", sa.String(length=64), nullable=False),
        sa.Column("context_sha256", sa.String(length=64), nullable=True),
        sa.Column("provider", sa.String(length=32), nullable=True),
        sa.Column("model", sa.String(length=128), nullable=True),
        sa.Column("candidate_payload", sa.JSON(), nullable=False),
        sa.Column("accepted_variant_ids", sa.JSON(), nullable=False),
        sa.Column("generation_metadata", sa.JSON(), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'completed', 'failed', 'ambiguous')",
            name="ck_packaging_candidate_generation_status",
        ),
        sa.ForeignKeyConstraint(["publication_id"], ["publications.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "publication_id",
            "generation_key",
            name="uq_packaging_candidate_generation_publication_key",
        ),
    )
    op.create_index(
        "ix_packaging_candidate_generations_publication_id",
        "packaging_candidate_generations",
        ["publication_id"],
    )
    op.create_index(
        "ix_packaging_candidate_generations_status",
        "packaging_candidate_generations",
        ["status"],
    )
    op.create_index(
        "ix_packaging_candidate_generations_created_at",
        "packaging_candidate_generations",
        ["created_at"],
    )


def downgrade() -> None:
    op.drop_table("packaging_candidate_generations")
