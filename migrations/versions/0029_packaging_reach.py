"""add YouTube reach reporting and packaging attribution

Revision ID: 0029_packaging_reach
Revises: 0028_publication_packaging
Create Date: 2026-09-24
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0029_packaging_reach"
down_revision: str | None = "0028_publication_packaging"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "youtube_reach_reporting_jobs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("youtube_connection_id", sa.Uuid(), nullable=False),
        sa.Column("report_type_id", sa.String(length=96), nullable=False),
        sa.Column("provider_job_id", sa.String(length=160), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("stage", sa.String(length=64), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("job_metadata", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("status IN ('registered', 'create_started', 'active', 'failed')", name="ck_youtube_reach_job_status"),
        sa.ForeignKeyConstraint(["youtube_connection_id"], ["youtube_connections.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("youtube_connection_id", "report_type_id", name="uq_youtube_reach_job_connection_type"),
    )
    op.create_index("ix_youtube_reach_reporting_jobs_youtube_connection_id", "youtube_reach_reporting_jobs", ["youtube_connection_id"])
    op.create_index("ix_youtube_reach_reporting_jobs_report_type_id", "youtube_reach_reporting_jobs", ["report_type_id"])
    op.create_index("ix_youtube_reach_reporting_jobs_provider_job_id", "youtube_reach_reporting_jobs", ["provider_job_id"])
    op.create_index("ix_youtube_reach_reporting_jobs_status", "youtube_reach_reporting_jobs", ["status"])

    op.create_table(
        "youtube_reach_report_imports",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("youtube_connection_id", sa.Uuid(), nullable=False),
        sa.Column("reach_job_id", sa.Uuid(), nullable=False),
        sa.Column("provider_report_id", sa.String(length=200), nullable=False),
        sa.Column("report_start", sa.DateTime(timezone=True), nullable=True),
        sa.Column("report_end", sa.DateTime(timezone=True), nullable=True),
        sa.Column("provider_created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("payload_sha256", sa.String(length=64), nullable=False),
        sa.Column("row_count", sa.BigInteger(), nullable=False),
        sa.Column("import_metadata", sa.JSON(), nullable=False),
        sa.Column("imported_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["youtube_connection_id"], ["youtube_connections.id"]),
        sa.ForeignKeyConstraint(["reach_job_id"], ["youtube_reach_reporting_jobs.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("youtube_connection_id", "provider_report_id", name="uq_youtube_reach_report_connection_provider"),
    )
    op.create_index("ix_youtube_reach_report_imports_youtube_connection_id", "youtube_reach_report_imports", ["youtube_connection_id"])
    op.create_index("ix_youtube_reach_report_imports_reach_job_id", "youtube_reach_report_imports", ["reach_job_id"])
    op.create_index("ix_youtube_reach_report_imports_provider_report_id", "youtube_reach_report_imports", ["provider_report_id"])

    op.create_table(
        "publication_reach_observations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("publication_id", sa.Uuid(), nullable=False),
        sa.Column("report_import_id", sa.Uuid(), nullable=False),
        sa.Column("report_date", sa.Date(), nullable=False),
        sa.Column("impressions", sa.BigInteger(), nullable=True),
        sa.Column("ctr", sa.Numeric(12, 8), nullable=True),
        sa.Column("attribution_status", sa.String(length=32), nullable=False),
        sa.Column("packaging_variant_id", sa.Uuid(), nullable=True),
        sa.Column("attribution_metadata", sa.JSON(), nullable=False),
        sa.Column("raw_row", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("attribution_status IN ('variant', 'legacy', 'mixed')", name="ck_publication_reach_attribution_status"),
        sa.ForeignKeyConstraint(["publication_id"], ["publications.id"]),
        sa.ForeignKeyConstraint(["report_import_id"], ["youtube_reach_report_imports.id"]),
        sa.ForeignKeyConstraint(["packaging_variant_id"], ["publication_packaging_variants.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("publication_id", "report_date", name="uq_publication_reach_observation_day"),
    )
    for column in ("publication_id", "report_import_id", "report_date", "attribution_status", "packaging_variant_id"):
        op.create_index(f"ix_publication_reach_observations_{column}", "publication_reach_observations", [column])


def downgrade() -> None:
    op.drop_table("publication_reach_observations")
    op.drop_table("youtube_reach_report_imports")
    op.drop_table("youtube_reach_reporting_jobs")
