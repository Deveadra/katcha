"""Durable versioned Editorial Storyboard workspaces."""

import sqlalchemy as sa
from alembic import op

revision = "0055_editorial_storyboards"
down_revision = "0054_editorial_images"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "editorial_storyboard_revisions",
        sa.Column(
            "project_id",
            sa.Uuid(),
            sa.ForeignKey("editorial_projects.id"),
            primary_key=True,
        ),
        sa.Column("script_revision", sa.Integer(), primary_key=True),
        sa.Column("version", sa.Integer(), primary_key=True),
        sa.Column(
            "channel_profile_id",
            sa.Uuid(),
            sa.ForeignKey("channel_profiles.id"),
            nullable=False,
        ),
        sa.Column("parent_version", sa.Integer(), nullable=True),
        sa.Column("request_id", sa.Uuid(), nullable=False, unique=True),
        sa.Column("request_digest", sa.String(64), nullable=False),
        sa.Column("digest", sa.String(64), nullable=False),
        sa.Column("workspace", sa.JSON(), nullable=False),
        sa.Column("origin", sa.String(32), nullable=False),
        sa.Column("actor", sa.String(255), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "script_revision > 0",
            name="ck_editorial_storyboard_script_revision",
        ),
        sa.CheckConstraint(
            "version > 0",
            name="ck_editorial_storyboard_version",
        ),
        sa.CheckConstraint(
            "parent_version IS NULL OR "
            "(parent_version > 0 AND parent_version < version)",
            name="ck_editorial_storyboard_parent_version",
        ),
        sa.CheckConstraint(
            "origin IN ('operator', 'ai_apply', 'undo')",
            name="ck_editorial_storyboard_origin",
        ),
    )
    op.create_index(
        "ix_editorial_storyboard_revisions_channel_profile_id",
        "editorial_storyboard_revisions",
        ["channel_profile_id"],
    )


def downgrade() -> None:
    op.drop_table("editorial_storyboard_revisions")
