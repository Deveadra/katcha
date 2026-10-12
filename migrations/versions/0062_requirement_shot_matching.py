"""Add immutable shot proposals and operator choice history; retain existing data."""

import sqlalchemy as sa
from alembic import op

revision = "0062_requirement_shot_matching"
down_revision = "0061_media_scene_indexes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for name in ("requirement_shot_packages", "operator_shot_choices"):
        columns = [
            sa.Column("id", sa.Uuid(), primary_key=True),
            sa.Column(
                "channel_profile_id",
                sa.Uuid(),
                sa.ForeignKey("channel_profiles.id"),
                nullable=False,
            ),
            sa.Column(
                "project_id", sa.Uuid(), sa.ForeignKey("editorial_projects.id"), nullable=False
            ),
            sa.Column("request_digest", sa.String(64), nullable=False),
            sa.Column("payload", sa.JSON(), nullable=False),
            sa.Column("actor", sa.String(255), nullable=False),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.func.now(),
            ),
        ]
        if name == "requirement_shot_packages":
            columns.extend(
                [
                    sa.Column("input_revision", sa.Integer(), nullable=False),
                    sa.Column("dependency_digest", sa.String(64), nullable=False),
                ]
            )
        else:
            columns.extend(
                [
                    sa.Column("requirement_id", sa.String(200), nullable=False),
                    sa.Column("version", sa.Integer(), nullable=False),
                    sa.UniqueConstraint(
                        "project_id", "requirement_id", "version", name="uq_shot_choice_version"
                    ),
                ]
            )
        op.create_table(name, *columns)


def downgrade() -> None:
    op.drop_table("operator_shot_choices")
    op.drop_table("requirement_shot_packages")
