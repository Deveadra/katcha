"""Append-only decisions bound to exact editorial render receipts."""

import sqlalchemy as sa
from alembic import op

revision = "0052_editorial_render_reviews"
down_revision = "0051_editorial_runs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "editorial_render_reviews",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("run_id", sa.Uuid(), sa.ForeignKey("editorial_runs.id"), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("request_digest", sa.String(64), nullable=False),
        sa.Column("manifest_digest", sa.String(64), nullable=False),
        sa.Column("result_digest", sa.String(64), nullable=False),
        sa.Column("decision", sa.String(32), nullable=False),
        sa.Column("note", sa.Text(), nullable=False),
        sa.Column("actor", sa.String(255), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint("run_id", "sequence", name="uq_editorial_review_sequence"),
        sa.CheckConstraint("sequence > 0", name="ck_editorial_review_sequence"),
        sa.CheckConstraint(
            "decision IN ('approve', 'request_changes')", name="ck_editorial_review_decision"
        ),
    )
    op.create_index("ix_editorial_render_reviews_run_id", "editorial_render_reviews", ["run_id"])


def downgrade() -> None:
    op.drop_table("editorial_render_reviews")
