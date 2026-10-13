"""Add an uninitialized authoritative search ledger; never replace historical evidence."""

import sqlalchemy as sa
from alembic import op

revision = "0063_authoritative_search_ledger"
down_revision = "0062_requirement_shot_matching"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Runtime cannot initialize this head. Only an explicit offline import creates it.
    op.create_table(
        "source_search_ledger_head",
        sa.Column("singleton", sa.Integer(), primary_key=True),
        sa.Column("ledger_id", sa.Text(), nullable=False),
        sa.Column("recovery_generation", sa.BigInteger(), nullable=False),
        sa.Column("integrity_sha256", sa.Text(), nullable=False),
        sa.Column("recovery_required", sa.Boolean(), nullable=False),
        sa.Column("activation_evidence", sa.Text(), nullable=False),
        sa.CheckConstraint("singleton = 1", name="ck_search_ledger_head_singleton"),
        sa.CheckConstraint("recovery_generation > 0", name="ck_search_ledger_head_epoch"),
    )
    op.create_table(
        "source_search_ledger_identity",
        sa.Column("singleton", sa.Integer(), primary_key=True),
        sa.Column("ledger_id", sa.Text(), nullable=False),
        sa.CheckConstraint("singleton = 1", name="ck_search_ledger_identity_singleton"),
    )
    op.create_table(
        "source_search_budgets",
        sa.Column("scope_key", sa.Text(), primary_key=True),
        sa.Column("provider", sa.Text(), primary_key=True),
        sa.Column("policy", sa.Text(), nullable=False),
        sa.Column("calls", sa.BigInteger(), nullable=False),
        sa.Column("results", sa.BigInteger(), nullable=False),
        sa.Column("cost", sa.BigInteger(), nullable=False),
        sa.CheckConstraint(
            "calls >= 0 AND results >= 0 AND cost >= 0", name="ck_search_budget_nonnegative"
        ),
    )
    op.create_table(
        "source_search_jobs",
        sa.Column("job_id", sa.Text(), primary_key=True),
        sa.Column("scope_key", sa.Text(), nullable=False),
        sa.Column("intent", sa.Text(), nullable=False),
        sa.Column("receipt", sa.Text(), nullable=False),
    )
    op.create_table(
        "source_search_candidates",
        sa.Column("job_id", sa.Text(), primary_key=True),
        sa.Column("candidate_id", sa.Text(), primary_key=True),
        sa.Column("scope_key", sa.Text(), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
    )
    op.create_table(
        "source_search_receipt_events",
        sa.Column("event_id", sa.BigInteger(), primary_key=True, autoincrement=False),
        sa.Column("scope_key", sa.Text(), nullable=False),
        sa.Column("job_id", sa.Text(), nullable=False),
        sa.Column("receipt", sa.Text(), nullable=False),
    )


def downgrade() -> None:
    raise RuntimeError(
        "Search receipts cannot be discarded by downgrade; retain and recover evidence"
    )
