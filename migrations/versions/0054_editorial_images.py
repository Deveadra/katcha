"""Revision-bound, permission-attested still images."""

import sqlalchemy as sa
from alembic import op

revision = "0054_editorial_images"
down_revision = "0053_editorial_narration"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "editorial_images",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("project_id", sa.Uuid(), sa.ForeignKey("editorial_projects.id"), nullable=False),
        sa.Column(
            "channel_profile_id", sa.Uuid(), sa.ForeignKey("channel_profiles.id"), nullable=False
        ),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("beat_id", sa.String(120), nullable=False),
        sa.Column("request_digest", sa.String(64), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("storage_key", sa.String(1000), nullable=False),
        sa.Column("width", sa.Integer(), nullable=False),
        sa.Column("height", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("source_reference", sa.String(2000), nullable=False),
        sa.Column("use_note", sa.String(2000), nullable=False),
        sa.Column("illustration", sa.Boolean(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("actor", sa.String(255), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("revision > 0", name="ck_editorial_image_revision"),
        sa.CheckConstraint("width > 0 AND height > 0", name="ck_editorial_image_dimensions"),
    )
    for column in ("project_id", "channel_profile_id"):
        op.create_index(f"ix_editorial_images_{column}", "editorial_images", [column])


def downgrade() -> None:
    op.drop_table("editorial_images")
