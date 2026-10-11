"""persist scoped media acquisition receipts

Revision ID: 0059_acquisition_receipts
Revises: 0058_project_media_bin
Create Date: 2026-10-11
"""

import sqlalchemy as sa
from alembic import op

revision = "0059_acquisition_receipts"
down_revision = "0058_project_media_bin"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "acquisition_receipts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("channel_profile_id", sa.Uuid(), nullable=False),
        sa.Column("candidate_id", sa.Uuid(), nullable=False),
        sa.Column("operation_key", sa.String(160), nullable=False),
        sa.Column("request_digest", sa.String(64), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("authorization_scope", sa.JSON(), nullable=False),
        sa.Column("fetch_url_if_allowed", sa.Text(), nullable=True),
        sa.Column("request_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("response_time", sa.DateTime(timezone=True), nullable=True),
        sa.Column("bytes_received", sa.Integer(), nullable=True),
        sa.Column("sha256", sa.String(64), nullable=True),
        sa.Column("mime", sa.String(160), nullable=True),
        sa.Column("media_probe", sa.JSON(), nullable=False),
        sa.Column("object_store_key", sa.Text(), nullable=True),
        sa.Column("duplicate_of", sa.Uuid(), nullable=True),
        sa.Column("source_version", sa.String(255), nullable=True),
        sa.Column("download_terms", sa.JSON(), nullable=False),
        sa.Column("cost_receipt", sa.JSON(), nullable=False),
        sa.Column("error_category", sa.String(80), nullable=True),
        sa.Column("safe_resume_cursor", sa.JSON(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "status IN ('authorized', 'acquiring', 'succeeded', 'failed', 'unknown')",
            name="ck_acquisition_receipt_status",
        ),
        sa.CheckConstraint(
            "bytes_received IS NULL OR bytes_received >= 0",
            name="ck_acquisition_receipt_bytes",
        ),
        sa.CheckConstraint(
            "status != 'succeeded' OR (sha256 IS NOT NULL AND object_store_key IS NOT NULL)",
            name="ck_acquisition_receipt_success_evidence",
        ),
        sa.CheckConstraint(
            "status != 'unknown' OR error_category IS NOT NULL",
            name="ck_acquisition_receipt_unknown_reason",
        ),
        sa.ForeignKeyConstraint(["channel_profile_id"], ["channel_profiles.id"]),
        sa.ForeignKeyConstraint(["candidate_id"], ["discovery_candidates.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "channel_profile_id", "operation_key", name="uq_acquisition_receipt_operation"
        ),
    )
    op.create_index(
        "ix_acquisition_receipts_channel_profile_id",
        "acquisition_receipts",
        ["channel_profile_id"],
    )
    op.create_index(
        "ix_acquisition_receipts_candidate_id", "acquisition_receipts", ["candidate_id"]
    )


def downgrade() -> None:
    op.drop_table("acquisition_receipts")
