"""Add channel-bound, fenced remote acquisition execution records."""

import sqlalchemy as sa
from alembic import op

revision = "0065_acquisition_jobs"
down_revision = "0064_application_ai_settings"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "acquisition_jobs",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("receipt_id", sa.Uuid(), nullable=False),
        sa.Column("channel_profile_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("max_bytes", sa.BigInteger(), nullable=False),
        sa.Column("bytes_received", sa.BigInteger(), nullable=False),
        sa.Column("heartbeat_sequence", sa.Integer(), nullable=False),
        sa.Column("worker_principal", sa.String(64)),
        sa.Column("claim_id", sa.Uuid()),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True)),
        sa.Column("last_heartbeat_at", sa.DateTime(timezone=True)),
        sa.Column("phase", sa.String(32), nullable=False),
        sa.Column("pending_result", sa.JSON(), nullable=False),
        sa.Column("failure_code", sa.String(64)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint("receipt_id", name="uq_acquisition_job_receipt"),
        sa.ForeignKeyConstraint(
            ["receipt_id", "channel_profile_id"],
            ["acquisition_receipts.id", "acquisition_receipts.channel_profile_id"],
            name="fk_acquisition_job_receipt_channel",
        ),
        sa.CheckConstraint(
            "status IN ('queued', 'leased', 'awaiting_validation', 'failed', "
            "'cancelled', 'recovery_required')",
            name="ck_acquisition_job_status",
        ),
        sa.CheckConstraint("generation >= 0", name="ck_acquisition_job_generation"),
        sa.CheckConstraint("max_bytes > 0", name="ck_acquisition_job_max_bytes"),
        sa.CheckConstraint(
            "bytes_received >= 0 AND bytes_received <= max_bytes",
            name="ck_acquisition_job_progress",
        ),
        sa.CheckConstraint("heartbeat_sequence >= 0", name="ck_acquisition_job_sequence"),
        sa.CheckConstraint(
            "status != 'leased' OR (worker_principal IS NOT NULL AND claim_id IS NOT NULL "
            "AND lease_expires_at IS NOT NULL AND generation > 0)",
            name="ck_acquisition_job_lease",
        ),
    )
    op.create_index(
        "ix_acquisition_jobs_channel_profile_id", "acquisition_jobs", ["channel_profile_id"]
    )
    op.create_index("ix_acquisition_jobs_status", "acquisition_jobs", ["status"])


def downgrade() -> None:
    op.drop_table("acquisition_jobs")
