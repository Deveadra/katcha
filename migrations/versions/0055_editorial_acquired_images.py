"""Lineage for automatically acquired Editorial stills."""

import sqlalchemy as sa
from alembic import op

revision = "0055_editorial_acquired_images"
down_revision = "0054_editorial_images"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "editorial_images",
        sa.Column(
            "discovery_candidate_id",
            sa.Uuid(),
            sa.ForeignKey("discovery_candidates.id"),
            nullable=True,
        ),
    )
    op.add_column(
        "editorial_images",
        sa.Column(
            "rights_assessment_id",
            sa.Uuid(),
            sa.ForeignKey("rights_assessments.id"),
            nullable=True,
        ),
    )
    op.add_column(
        "editorial_images",
        sa.Column(
            "acquisition_run_id",
            sa.Uuid(),
            sa.ForeignKey("editorial_runs.id"),
            nullable=True,
        ),
    )
    for column in (
        "discovery_candidate_id",
        "rights_assessment_id",
        "acquisition_run_id",
    ):
        op.create_index(
            f"ix_editorial_images_{column}",
            "editorial_images",
            [column],
        )


def downgrade() -> None:
    for column in (
        "acquisition_run_id",
        "rights_assessment_id",
        "discovery_candidate_id",
    ):
        op.drop_index(f"ix_editorial_images_{column}", table_name="editorial_images")
        op.drop_column("editorial_images", column)
