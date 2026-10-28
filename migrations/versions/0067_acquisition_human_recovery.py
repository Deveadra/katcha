"""Bounded human verification and encrypted browser command mailbox."""

import sqlalchemy as sa
from alembic import op

revision = "0067_acquisition_human_recovery"
down_revision = "0066_acquisition_handoff"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("acquisition_jobs") as batch:
        batch.drop_constraint("ck_acquisition_job_status", type_="check")
        batch.create_check_constraint(
            "ck_acquisition_job_status",
            "status IN ('queued', 'leased', 'awaiting_validation', 'failed', "
            "'cancelled', 'recovery_required', 'waiting_human', 'completed')",
        )
    op.create_table(
        "acquisition_challenges",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("job_id", sa.Uuid(), nullable=False),
        sa.Column("channel_profile_id", sa.Uuid(), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column("code", sa.String(64), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("notification_state", sa.String(32), nullable=False),
        sa.Column("notification_message_id", sa.BigInteger(), nullable=True),
        sa.Column("notification_updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["job_id"], ["acquisition_jobs.id"]),
        sa.UniqueConstraint("job_id", "generation", name="uq_acquisition_challenge_generation"),
        sa.CheckConstraint(
            "state IN ('waiting', 'retry_requested', 'retrying', 'closed')",
            name="ck_acquisition_challenge_state",
        ),
    )
    op.create_index("ix_acquisition_challenges_job_id", "acquisition_challenges", ["job_id"])
    op.create_index(
        "ix_acquisition_challenges_channel_profile_id",
        "acquisition_challenges",
        ["channel_profile_id"],
    )
    op.create_table(
        "acquisition_browser_commands",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("challenge_id", sa.Uuid(), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("payload", sa.Text(), nullable=True),
        sa.Column("result", sa.Text(), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["challenge_id"], ["acquisition_challenges.id"]),
        sa.CheckConstraint(
            "state IN ('queued', 'claimed', 'completed', 'expired')",
            name="ck_acquisition_browser_command_state",
        ),
    )
    op.create_index(
        "ix_acquisition_browser_commands_challenge_id",
        "acquisition_browser_commands",
        ["challenge_id"],
    )


def downgrade():
    if op.get_bind().execute(sa.text("SELECT COUNT(*) FROM acquisition_challenges")).scalar_one():
        raise RuntimeError(
            "Retain human recovery history; do not downgrade populated recovery tables"
        )
    op.drop_table("acquisition_browser_commands")
    op.drop_table("acquisition_challenges")
    with op.batch_alter_table("acquisition_jobs") as batch:
        batch.drop_constraint("ck_acquisition_job_status", type_="check")
        batch.create_check_constraint(
            "ck_acquisition_job_status",
            "status IN ('queued', 'leased', 'awaiting_validation', 'failed', "
            "'cancelled', 'recovery_required', 'completed')",
        )
