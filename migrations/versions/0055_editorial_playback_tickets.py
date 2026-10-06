"""Short-lived same-origin Editorial source playback grants."""

import sqlalchemy as sa
from alembic import op

revision = "0055_editorial_playback_tickets"
down_revision = "0054_editorial_images"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "editorial_playback_tickets",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "channel_profile_id",
            sa.Uuid(),
            sa.ForeignKey("channel_profiles.id"),
            nullable=False,
        ),
        sa.Column(
            "project_id",
            sa.Uuid(),
            sa.ForeignKey("editorial_projects.id"),
            nullable=False,
        ),
        sa.Column(
            "run_id",
            sa.Uuid(),
            sa.ForeignKey("editorial_runs.id"),
            nullable=False,
        ),
        sa.Column("clip_id", sa.Uuid(), sa.ForeignKey("clips.id"), nullable=False),
        sa.Column("candidate_id", sa.String(200), nullable=False),
        sa.Column("token_digest", sa.String(64), nullable=False, unique=True),
        sa.Column("actor", sa.String(255), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    for column in (
        "channel_profile_id",
        "project_id",
        "run_id",
        "clip_id",
        "expires_at",
    ):
        op.create_index(
            f"ix_editorial_playback_tickets_{column}",
            "editorial_playback_tickets",
            [column],
        )


def downgrade() -> None:
    op.drop_table("editorial_playback_tickets")
