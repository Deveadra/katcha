"""add staged brand preview render ledger

Revision ID: 0034_brand_preview_renders
Revises: 0033_channel_brand_control
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0034_brand_preview_renders"
down_revision: str | None = "0033_channel_brand_control"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "brand_preview_renders",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "channel_profile_id",
            sa.Uuid(),
            sa.ForeignKey("channel_profiles.id"),
            nullable=False,
        ),
        sa.Column(
            "production_id",
            sa.Uuid(),
            sa.ForeignKey("productions.id"),
            nullable=False,
        ),
        sa.Column(
            "brand_version_id",
            sa.Uuid(),
            sa.ForeignKey("channel_brand_versions.id"),
            nullable=False,
        ),
        sa.Column("brand_key", sa.String(64), nullable=False),
        sa.Column("brand_version", sa.Integer(), nullable=False),
        sa.Column("request_key", sa.String(64), nullable=False),
        sa.Column("workflow_id", sa.String(255), nullable=False),
        sa.Column("workflow_attempt", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("source_lineage", sa.JSON(), nullable=False),
        sa.Column("brand_snapshot", sa.JSON(), nullable=False),
        sa.Column("reaction_cue", sa.JSON(), nullable=True),
        sa.Column("render_manifest", sa.JSON(), nullable=False),
        sa.Column("output_key", sa.Text(), nullable=True),
        sa.Column("verification", sa.JSON(), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
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
        sa.UniqueConstraint(
            "channel_profile_id",
            "request_key",
            name="uq_brand_preview_channel_request",
        ),
        sa.UniqueConstraint("workflow_id"),
    )
    for column in (
        "channel_profile_id",
        "production_id",
        "brand_version_id",
        "brand_key",
        "brand_version",
        "workflow_id",
        "status",
    ):
        op.create_index(
            f"ix_brand_preview_renders_{column}",
            "brand_preview_renders",
            [column],
        )


def downgrade() -> None:
    for column in (
        "status",
        "workflow_id",
        "brand_version",
        "brand_key",
        "brand_version_id",
        "production_id",
        "channel_profile_id",
    ):
        op.drop_index(
            f"ix_brand_preview_renders_{column}",
            table_name="brand_preview_renders",
        )
    op.drop_table("brand_preview_renders")
