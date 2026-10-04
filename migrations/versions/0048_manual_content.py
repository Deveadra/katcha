"""Add persistent manual content receipts without rewriting media or existing history."""

import sqlalchemy as sa
from alembic import op

revision = "0048_manual_content"
down_revision = "0047_packaging_seo_fields"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "content_items",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "channel_profile_id", sa.Uuid(), sa.ForeignKey("channel_profiles.id"), nullable=False
        ),
        sa.Column("request_key", sa.String(160), nullable=False),
        sa.Column("input_kind", sa.String(32), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("source_id", sa.Uuid(), sa.ForeignKey("source_items.id")),
        sa.Column("clip_id", sa.Uuid(), sa.ForeignKey("clips.id")),
        sa.Column("production_id", sa.Uuid(), sa.ForeignKey("productions.id")),
        sa.Column("filename", sa.Text()),
        sa.Column("size_bytes", sa.BigInteger()),
        sa.Column("upload_offset", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("details", sa.JSON(), nullable=False),
        sa.Column("error", sa.Text()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint("channel_profile_id", "request_key", name="uq_content_channel_request"),
    )
    for column in ("channel_profile_id", "status", "source_id", "clip_id"):
        op.create_index(f"ix_content_items_{column}", "content_items", [column])


def downgrade() -> None:
    op.drop_table("content_items")
