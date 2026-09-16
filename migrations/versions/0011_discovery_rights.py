"""add discovery provenance and rights qualification

Revision ID: 0011_discovery_rights
Revises: 0010_budget_reservations
Create Date: 2026-09-16
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0011_discovery_rights"
down_revision: str | None = "0010_budget_reservations"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "discovery_runs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("adapter_key", sa.String(length=64), nullable=False),
        sa.Column("adapter_version", sa.String(length=64), nullable=False),
        sa.Column("run_key", sa.String(length=160), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("query", sa.JSON(), nullable=False),
        sa.Column("cursor", sa.JSON(), nullable=False),
        sa.Column("run_metadata", sa.JSON(), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("adapter_key", "run_key"),
    )
    op.create_index("ix_discovery_runs_adapter_key", "discovery_runs", ["adapter_key"])
    op.create_index("ix_discovery_runs_run_key", "discovery_runs", ["run_key"])
    op.create_index("ix_discovery_runs_status", "discovery_runs", ["status"])

    op.create_table(
        "discovery_candidates",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("discovery_run_id", sa.Uuid(), nullable=True),
        sa.Column("source_item_id", sa.Uuid(), nullable=True),
        sa.Column("adapter_key", sa.String(length=64), nullable=False),
        sa.Column("external_id", sa.String(length=255), nullable=True),
        sa.Column("source_url", sa.Text(), nullable=False),
        sa.Column("canonical_url", sa.Text(), nullable=False),
        sa.Column("platform", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column("creator", sa.Text(), nullable=True),
        sa.Column("creator_url", sa.Text(), nullable=True),
        sa.Column(
            "provenance_confidence",
            sa.Numeric(8, 6),
            nullable=False,
        ),
        sa.Column("provenance_claims", sa.JSON(), nullable=False),
        sa.Column("candidate_metadata", sa.JSON(), nullable=False),
        sa.Column(
            "discovered_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "provenance_confidence >= 0 AND provenance_confidence <= 1",
            name="ck_discovery_provenance_confidence",
        ),
        sa.ForeignKeyConstraint(["discovery_run_id"], ["discovery_runs.id"]),
        sa.ForeignKeyConstraint(["source_item_id"], ["source_items.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("adapter_key", "external_id"),
        sa.UniqueConstraint("canonical_url"),
        sa.UniqueConstraint("source_item_id"),
    )
    op.create_index(
        "ix_discovery_candidates_discovery_run_id",
        "discovery_candidates",
        ["discovery_run_id"],
    )
    op.create_index(
        "ix_discovery_candidates_source_item_id",
        "discovery_candidates",
        ["source_item_id"],
    )
    op.create_index(
        "ix_discovery_candidates_adapter_key",
        "discovery_candidates",
        ["adapter_key"],
    )
    op.create_index(
        "ix_discovery_candidates_external_id",
        "discovery_candidates",
        ["external_id"],
    )
    op.create_index(
        "ix_discovery_candidates_platform",
        "discovery_candidates",
        ["platform"],
    )
    op.create_index(
        "ix_discovery_candidates_status",
        "discovery_candidates",
        ["status"],
    )
    op.create_index(
        "ix_discovery_candidates_discovered_at",
        "discovery_candidates",
        ["discovered_at"],
    )

    op.create_table(
        "rights_assessments",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("discovery_candidate_id", sa.Uuid(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("rights_basis", sa.String(length=64), nullable=False),
        sa.Column("rights_lane", sa.String(length=16), nullable=False),
        sa.Column("rights_gate", sa.String(length=32), nullable=False),
        sa.Column("audio_status", sa.String(length=32), nullable=False),
        sa.Column("originality_gate", sa.String(length=32), nullable=False),
        sa.Column("production_eligible", sa.Boolean(), nullable=False),
        sa.Column("operator_authorized", sa.Boolean(), nullable=False),
        sa.Column("risk_flags", sa.JSON(), nullable=False),
        sa.Column("fair_use_factors", sa.JSON(), nullable=False),
        sa.Column("assessment_metadata", sa.JSON(), nullable=False),
        sa.Column("actor", sa.String(length=128), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "version > 0",
            name="ck_rights_assessment_version_positive",
        ),
        sa.ForeignKeyConstraint(
            ["discovery_candidate_id"],
            ["discovery_candidates.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("discovery_candidate_id", "version"),
    )
    for name in (
        "discovery_candidate_id",
        "rights_basis",
        "rights_lane",
        "rights_gate",
        "audio_status",
        "originality_gate",
        "production_eligible",
        "created_at",
    ):
        op.create_index(
            f"ix_rights_assessments_{name}",
            "rights_assessments",
            [name],
        )

    op.create_table(
        "rights_evidence",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("rights_assessment_id", sa.Uuid(), nullable=False),
        sa.Column("evidence_type", sa.String(length=64), nullable=False),
        sa.Column("source_url", sa.Text(), nullable=True),
        sa.Column("snapshot_key", sa.Text(), nullable=True),
        sa.Column("content_sha256", sa.String(length=64), nullable=True),
        sa.Column("terms_version", sa.String(length=128), nullable=True),
        sa.Column("evidence_metadata", sa.JSON(), nullable=False),
        sa.Column(
            "captured_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["rights_assessment_id"],
            ["rights_assessments.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_rights_evidence_rights_assessment_id",
        "rights_evidence",
        ["rights_assessment_id"],
    )
    op.create_index(
        "ix_rights_evidence_evidence_type",
        "rights_evidence",
        ["evidence_type"],
    )
    op.create_index(
        "ix_rights_evidence_content_sha256",
        "rights_evidence",
        ["content_sha256"],
    )
    op.create_index(
        "ix_rights_evidence_captured_at",
        "rights_evidence",
        ["captured_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_rights_evidence_captured_at", table_name="rights_evidence")
    op.drop_index("ix_rights_evidence_content_sha256", table_name="rights_evidence")
    op.drop_index("ix_rights_evidence_evidence_type", table_name="rights_evidence")
    op.drop_index(
        "ix_rights_evidence_rights_assessment_id",
        table_name="rights_evidence",
    )
    op.drop_table("rights_evidence")

    for name in reversed(
        (
            "discovery_candidate_id",
            "rights_basis",
            "rights_lane",
            "rights_gate",
            "audio_status",
            "originality_gate",
            "production_eligible",
            "created_at",
        )
    ):
        op.drop_index(
            f"ix_rights_assessments_{name}",
            table_name="rights_assessments",
        )
    op.drop_table("rights_assessments")

    op.drop_index(
        "ix_discovery_candidates_discovered_at",
        table_name="discovery_candidates",
    )
    op.drop_index("ix_discovery_candidates_status", table_name="discovery_candidates")
    op.drop_index("ix_discovery_candidates_platform", table_name="discovery_candidates")
    op.drop_index(
        "ix_discovery_candidates_external_id",
        table_name="discovery_candidates",
    )
    op.drop_index(
        "ix_discovery_candidates_adapter_key",
        table_name="discovery_candidates",
    )
    op.drop_index(
        "ix_discovery_candidates_source_item_id",
        table_name="discovery_candidates",
    )
    op.drop_index(
        "ix_discovery_candidates_discovery_run_id",
        table_name="discovery_candidates",
    )
    op.drop_table("discovery_candidates")

    op.drop_index("ix_discovery_runs_status", table_name="discovery_runs")
    op.drop_index("ix_discovery_runs_run_key", table_name="discovery_runs")
    op.drop_index("ix_discovery_runs_adapter_key", table_name="discovery_runs")
    op.drop_table("discovery_runs")
