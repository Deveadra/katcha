"""media intelligence tables

Revision ID: 0002_media_intelligence
Revises: 0001_foundation
Create Date: 2026-09-15
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002_media_intelligence"
down_revision: str | None = "0001_foundation"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "clip_analysis_runs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("clip_id", sa.Uuid(), nullable=False),
        sa.Column("workflow_id", sa.String(length=255), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("stage", sa.String(length=64), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("escalation_reason", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["clip_id"], ["clips.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("workflow_id"),
    )
    op.create_index("ix_clip_analysis_runs_clip_id", "clip_analysis_runs", ["clip_id"])
    op.create_index("ix_clip_analysis_runs_status", "clip_analysis_runs", ["status"])
    op.create_index("ix_clip_analysis_runs_workflow_id", "clip_analysis_runs", ["workflow_id"])

    op.create_table(
        "clip_features",
        sa.Column("clip_id", sa.Uuid(), nullable=False),
        sa.Column("contact_sheet_key", sa.Text(), nullable=True),
        sa.Column("keyframe_keys", sa.JSON(), nullable=False),
        sa.Column("perceptual_hashes", sa.JSON(), nullable=False),
        sa.Column("transcript", sa.Text(), nullable=True),
        sa.Column("transcript_language", sa.String(length=32), nullable=True),
        sa.Column("transcript_confidence", sa.Numeric(8, 6), nullable=True),
        sa.Column("local_features", sa.JSON(), nullable=False),
        sa.Column("ai_features", sa.JSON(), nullable=False),
        sa.Column("candidate_score", sa.Numeric(6, 3), nullable=True),
        sa.Column("score_breakdown", sa.JSON(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["clip_id"], ["clips.id"]),
        sa.PrimaryKeyConstraint("clip_id"),
    )


def downgrade() -> None:
    op.drop_table("clip_features")
    op.drop_index("ix_clip_analysis_runs_workflow_id", table_name="clip_analysis_runs")
    op.drop_index("ix_clip_analysis_runs_status", table_name="clip_analysis_runs")
    op.drop_index("ix_clip_analysis_runs_clip_id", table_name="clip_analysis_runs")
    op.drop_table("clip_analysis_runs")
