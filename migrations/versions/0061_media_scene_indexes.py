"""Add durable measured media indexes without modifying existing media or rights."""

import sqlalchemy as sa
from alembic import op

revision = "0061_media_scene_indexes"
down_revision = "0060_media_asset_validations"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "media_scene_indexes",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("channel_profile_id", sa.Uuid(), nullable=False),
        sa.Column("clip_id", sa.Uuid(), nullable=False),
        sa.Column("clip_sha256", sa.String(64), nullable=False),
        sa.Column("policy_version", sa.String(80), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("evidence", sa.JSON(), nullable=False),
        sa.Column("receipt", sa.JSON(), nullable=False),
        sa.Column("error_category", sa.String(80), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["channel_profile_id"], ["channel_profiles.id"]),
        sa.ForeignKeyConstraint(["clip_id"], ["clips.id"]),
        sa.UniqueConstraint(
            "channel_profile_id",
            "clip_id",
            "clip_sha256",
            "policy_version",
            name="uq_scene_index_content_policy",
        ),
        sa.CheckConstraint(
            "status IN ('indexing', 'completed', 'failed')", name="ck_scene_index_status"
        ),
    )
    op.create_index(
        "ix_media_scene_indexes_channel_profile_id", "media_scene_indexes", ["channel_profile_id"]
    )


def downgrade() -> None:
    op.drop_table("media_scene_indexes")
