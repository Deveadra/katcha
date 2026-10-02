"""Persist recurring automation schedules for disaster reconstruction."""

import sqlalchemy as sa
from alembic import op

revision = "0049_automation_schedules"
down_revision = "0048_manual_content"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "automation_schedules",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("schedule_key", sa.String(255), nullable=False),
        sa.Column("schedule_kind", sa.String(64), nullable=False),
        sa.Column("subject_id", sa.Uuid(), nullable=False),
        sa.Column("workflow_id", sa.String(255), nullable=False),
        sa.Column("supersedes_workflow_id", sa.String(255)),
        sa.Column("generation", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("schedule_config", sa.JSON(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "generation > 0",
            name="ck_automation_schedule_generation_positive",
        ),
        sa.UniqueConstraint(
            "schedule_key",
            name="uq_automation_schedule_key",
        ),
        sa.UniqueConstraint(
            "workflow_id",
            name="uq_automation_schedule_workflow",
        ),
    )
    for column in (
        "schedule_key",
        "schedule_kind",
        "subject_id",
        "workflow_id",
        "enabled",
    ):
        op.create_index(
            f"ix_automation_schedules_{column}",
            "automation_schedules",
            [column],
        )


def downgrade() -> None:
    op.drop_table("automation_schedules")
