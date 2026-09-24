"""add guarded packaging experiment ledger

Revision ID: 0032_packaging_experiments
Revises: 0031_packaging_candidate_generation
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
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("publication_id", sa.Uuid(), sa.ForeignKey("publications.id"), nullable=False),
        sa.Column(
            "channel_profile_id", sa.Uuid(), sa.ForeignKey("channel_profiles.id"), nullable=False
        ),
        sa.Column("experiment_key", sa.String(160), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column(
            "candidate_variant_id",
            sa.Uuid(),
            sa.ForeignKey("publication_packaging_variants.id"),
            nullable=False,
        ),
        sa.Column(
            "previous_variant_id",
            sa.Uuid(),
            sa.ForeignKey("publication_packaging_variants.id"),
            nullable=False,
        ),
        sa.Column(
            "snapshot_id",
            sa.Uuid(),
            sa.ForeignKey("packaging_intelligence_snapshots.id"),
            nullable=False,
        ),
        sa.Column(
            "baseline_window_id",
            sa.Uuid(),
            sa.ForeignKey("packaging_variant_performance_windows.id"),
            nullable=False,
        ),
        sa.Column(
            "activation_id",
            sa.Uuid(),
            sa.ForeignKey("publication_packaging_activations.id"),
            nullable=True,
        ),
        sa.Column(
            "rollback_activation_id",
            sa.Uuid(),
            sa.ForeignKey("publication_packaging_activations.id"),
            nullable=True,
        ),
        sa.Column("evidence", sa.JSON(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "status IN ('preparing', 'pending', 'observing', 'rollback_pending', 'rolled_back', 'completed', 'needs_review')",
            name="ck_packaging_experiment_status",
        ),
        sa.UniqueConstraint("publication_id", "experiment_key", name="uq_packaging_experiment_key"),
    )
    for column in ("publication_id", "channel_profile_id", "status"):
        op.create_index(f"ix_packaging_experiments_{column}", "packaging_experiments", [column])
    op.create_index(
        "uq_packaging_experiment_active",
        "packaging_experiments",
        ["publication_id"],
        unique=True,
        postgresql_where=sa.text(
            "status IN ('preparing','pending','observing','rollback_pending')"
        ),
        sqlite_where=sa.text("status IN ('preparing','pending','observing','rollback_pending')"),
    )


def downgrade() -> None:
    op.drop_index("uq_packaging_experiment_active", table_name="packaging_experiments")
    for column in ("status", "channel_profile_id", "publication_id"):
        op.drop_index(f"ix_packaging_experiments_{column}", table_name="packaging_experiments")
    op.drop_table("packaging_experiments")
