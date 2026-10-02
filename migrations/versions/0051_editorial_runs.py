"""Persist editorial execution intent, attempt fencing and checkpoints."""

import sqlalchemy as sa
from alembic import op

revision = "0051_editorial_runs"
down_revision = "0050_editorial_projects"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "editorial_runs",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("project_id", sa.Uuid(), sa.ForeignKey("editorial_projects.id"), nullable=False),
        sa.Column(
            "channel_profile_id", sa.Uuid(), sa.ForeignKey("channel_profiles.id"), nullable=False
        ),
        sa.Column("input_digest", sa.String(64), nullable=False),
        sa.Column("input_revision", sa.Integer(), nullable=False),
        sa.Column("options", sa.JSON(), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("stage", sa.String(64), nullable=False),
        sa.Column("artifacts", sa.JSON(), nullable=False),
        sa.Column("error", sa.Text()),
        sa.Column("actor", sa.String(255), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint("attempt > 0", name="ck_editorial_run_attempt"),
    )
    for column in ("project_id", "channel_profile_id", "status"):
        op.create_index(f"ix_editorial_runs_{column}", "editorial_runs", [column])


def downgrade() -> None:
    op.drop_table("editorial_runs")
