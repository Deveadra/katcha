"""add guarded packaging experiments

Revision ID: 0032_packaging_experiments
Revises: 0031_packaging_candidate_generation
Create Date: 2026-09-24
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0032_packaging_experiments"
down_revision: str | None = "0031_packaging_candidate_generation"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "packaging_experiments",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("publication_id", sa.Uuid(), nullable=False),
        sa.Column("experiment_key", sa.String(length=160), nullable=False),
        sa.Column("baseline_variant_id", sa.Uuid(), nullable=False),
        sa.Column("candidate_variant_id", sa.Uuid(), nullable=False),
        sa.Column("intelligence_snapshot_id", sa.Uuid(), nullable=False),
        sa.Column("automation_version", sa.Integer(), nullable=False),
        sa.Column("start_activation_id", sa.Uuid(), nullable=True),
        sa.Column("rollback_activation_id", sa.Uuid(), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("stage", sa.String(length=64), nullable=False),
        sa.Column("observe_after", sa.DateTime(timezone=True), nullable=False),
        sa.Column("evidence_snapshot", sa.JSON(), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ("
            "'planned', 'activating', 'observing', 'rollback_queued', "
            "'rolled_back', 'completed', 'failed'"
            ")",
            name="ck_packaging_experiment_status",
        ),
        sa.ForeignKeyConstraint(["publication_id"], ["publications.id"]),
        sa.ForeignKeyConstraint(
            ["baseline_variant_id"],
            ["publication_packaging_variants.id"],
        ),
        sa.ForeignKeyConstraint(
            ["candidate_variant_id"],
            ["publication_packaging_variants.id"],
        ),
        sa.ForeignKeyConstraint(
            ["intelligence_snapshot_id"],
            ["packaging_intelligence_snapshots.id"],
        ),
        sa.ForeignKeyConstraint(
            ["start_activation_id"],
            ["publication_packaging_activations.id"],
        ),
        sa.ForeignKeyConstraint(
            ["rollback_activation_id"],
            ["publication_packaging_activations.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "publication_id",
            "experiment_key",
            name="uq_packaging_experiment_publication_key",
        ),
    )
    for column in (
        "publication_id",
        "baseline_variant_id",
        "candidate_variant_id",
        "intelligence_snapshot_id",
        "start_activation_id",
        "rollback_activation_id",
        "status",
        "observe_after",
        "created_at",
    ):
        op.create_index(
            f"ix_packaging_experiments_{column}",
            "packaging_experiments",
            [column],
        )


def downgrade() -> None:
    op.drop_table("packaging_experiments")
