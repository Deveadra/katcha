"""Durable multipart handoff and verified remote acquisition outcomes."""

import sqlalchemy as sa
from alembic import op

revision = "0066_acquisition_handoff"
down_revision = "0065_acquisition_jobs"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("acquisition_jobs") as batch:
        batch.add_column(
            sa.Column("media_type", sa.String(16), nullable=False, server_default="video")
        )
        batch.add_column(sa.Column("source_item_id", sa.Uuid(), nullable=True))
        batch.drop_constraint("ck_acquisition_job_status", type_="check")
        batch.create_check_constraint(
            "ck_acquisition_job_status",
            "status IN ('queued', "
            "'leased', 'awaiting_validation', 'failed', 'cancelled', "
            "'recovery_required', 'completed')",
        )
    # Historic receipts/validation sizes must hold 3GB+ media on PostgreSQL too.
    with op.batch_alter_table("acquisition_receipts") as batch:
        batch.alter_column("bytes_received", existing_type=sa.Integer(), type_=sa.BigInteger())
    with op.batch_alter_table("media_asset_validations") as batch:
        batch.alter_column("size_bytes", existing_type=sa.Integer(), type_=sa.BigInteger())
    op.create_table(
        "acquisition_transfers",
        sa.Column("job_id", sa.Uuid(), primary_key=True),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("upload_id", sa.Text(), nullable=False),
        sa.Column("parts", sa.JSON(), nullable=False),
        sa.Column("completed", sa.Boolean(), nullable=False),
        sa.ForeignKeyConstraint(["job_id"], ["acquisition_jobs.id"]),
        sa.CheckConstraint("size_bytes > 0", name="ck_acquisition_transfer_size"),
    )


def downgrade():
    if (
        op.get_bind()
        .execute(sa.text("SELECT COUNT(*) FROM acquisition_jobs WHERE status = 'completed'"))
        .scalar_one()
    ):
        raise RuntimeError("Cannot downgrade while completed acquisition records exist")
    if op.get_bind().execute(sa.text("SELECT COUNT(*) FROM acquisition_transfers")).scalar_one():
        raise RuntimeError("Cannot downgrade while durable acquisition transfer cursors exist")
    # Retain widened historic byte counters: narrowing could destroy valid data.
    op.drop_table("acquisition_transfers")
    with op.batch_alter_table("acquisition_jobs") as batch:
        batch.drop_column("source_item_id")
        batch.drop_column("media_type")
        batch.drop_constraint("ck_acquisition_job_status", type_="check")
        batch.create_check_constraint(
            "ck_acquisition_job_status",
            "status IN ('queued', "
            "'leased', 'awaiting_validation', 'failed', 'cancelled', 'recovery_required')",
        )
