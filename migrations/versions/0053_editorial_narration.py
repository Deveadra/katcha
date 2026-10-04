"""Revision-bound measured operator narration."""

import sqlalchemy as sa
from alembic import op

revision = "0053_editorial_narration"
down_revision = "0052_editorial_render_reviews"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "editorial_narration",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("project_id", sa.Uuid(), sa.ForeignKey("editorial_projects.id"), nullable=False),
        sa.Column(
            "channel_profile_id", sa.Uuid(), sa.ForeignKey("channel_profiles.id"), nullable=False
        ),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("beat_id", sa.String(120), nullable=False),
        sa.Column("text_digest", sa.String(64), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("storage_key", sa.String(1000), nullable=False),
        sa.Column("sample_rate", sa.Integer(), nullable=False),
        sa.Column("sample_frames", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("actor", sa.String(255), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint("revision > 0", name="ck_editorial_narration_revision"),
        sa.CheckConstraint("sample_frames > 0", name="ck_editorial_narration_frames"),
    )
    for column in ("project_id", "channel_profile_id"):
        op.create_index(f"ix_editorial_narration_{column}", "editorial_narration", [column])


def downgrade() -> None:
    op.drop_table("editorial_narration")
