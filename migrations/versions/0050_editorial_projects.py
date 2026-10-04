"""Add channel-scoped editorial drafts without changing compilations."""

import sqlalchemy as sa
from alembic import op

revision = "0050_editorial_projects"
down_revision = "0049_automation_schedules"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "editorial_projects",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "channel_profile_id", sa.Uuid(), sa.ForeignKey("channel_profiles.id"), nullable=False
        ),
        sa.Column("input_digest", sa.String(64), nullable=False),
        sa.Column("brief", sa.JSON(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint("revision >= 0", name="ck_editorial_project_revision"),
    )
    op.create_index(
        "ix_editorial_projects_channel_profile_id", "editorial_projects", ["channel_profile_id"]
    )
    op.create_table(
        "editorial_revisions",
        sa.Column(
            "project_id", sa.Uuid(), sa.ForeignKey("editorial_projects.id"), primary_key=True
        ),
        sa.Column("revision", sa.Integer(), primary_key=True),
        sa.Column("request_id", sa.Uuid(), nullable=False, unique=True),
        sa.Column("request_digest", sa.String(64), nullable=False),
        sa.Column("digest", sa.String(64), nullable=False),
        sa.Column("draft", sa.JSON(), nullable=False),
        sa.Column("actor", sa.String(255), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint("revision > 0", name="ck_editorial_revision_positive"),
    )


def downgrade() -> None:
    op.drop_table("editorial_revisions")
    op.drop_table("editorial_projects")
