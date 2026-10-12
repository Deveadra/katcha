"""add channel-scoped media integrity probe evidence

Revision ID: 0060_media_asset_validations
Revises: 0059_acquisition_receipts
Create Date: 2026-10-11
"""

import sqlalchemy as sa
from alembic import op

revision = "0060_media_asset_validations"
down_revision = "0059_acquisition_receipts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_unique_constraint(
        "uq_acquisition_receipt_channel_scope",
        "acquisition_receipts",
        ["id", "channel_profile_id"],
    )
    op.create_table(
        "media_asset_validations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("receipt_id", sa.Uuid(), nullable=False),
        sa.Column("channel_profile_id", sa.Uuid(), nullable=False),
        sa.Column("probe_version", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("media_type", sa.String(16), nullable=False),
        sa.Column("expected_sha256", sa.String(64), nullable=False),
        sa.Column("actual_sha256", sa.String(64), nullable=False),
        sa.Column("perceptual_hash", sa.String(32), nullable=True),
        sa.Column("duplicate_of_id", sa.Uuid(), nullable=True),
        sa.Column("mime", sa.String(160), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("duration_seconds", sa.Float(), nullable=True),
        sa.Column("width", sa.Integer(), nullable=True),
        sa.Column("height", sa.Integer(), nullable=True),
        sa.Column("probe", sa.JSON(), nullable=False),
        sa.Column("error_category", sa.String(80), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("probe_version > 0", name="ck_media_validation_probe_version"),
        sa.CheckConstraint("size_bytes > 0", name="ck_media_validation_size_positive"),
        sa.CheckConstraint(
            "status IN ('validated', 'rejected')", name="ck_media_validation_status"
        ),
        sa.CheckConstraint(
            "media_type IN ('video', 'image', 'audio')", name="ck_media_validation_type"
        ),
        sa.ForeignKeyConstraint(
            ["receipt_id", "channel_profile_id"],
            ["acquisition_receipts.id", "acquisition_receipts.channel_profile_id"],
            name="fk_media_validation_receipt_channel",
        ),
        sa.ForeignKeyConstraint(["channel_profile_id"], ["channel_profiles.id"]),
        sa.ForeignKeyConstraint(
            ["duplicate_of_id", "channel_profile_id"],
            ["media_asset_validations.id", "media_asset_validations.channel_profile_id"],
            name="fk_media_validation_duplicate_channel",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "receipt_id", "probe_version", name="uq_media_validation_receipt_version"
        ),
        sa.UniqueConstraint("id", "channel_profile_id", name="uq_media_validation_channel_scope"),
    )
    op.create_index(
        "ix_media_asset_validations_channel_profile_id",
        "media_asset_validations",
        ["channel_profile_id"],
    )


def downgrade() -> None:
    op.drop_table("media_asset_validations")
    op.drop_constraint(
        "uq_acquisition_receipt_channel_scope", "acquisition_receipts", type_="unique"
    )
