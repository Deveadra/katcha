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
        sa.Column("report_type_id", sa.String(length=128), nullable=False),
        sa.Column("provider_job_id", sa.String(length=255), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("job_metadata", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "status IN ('pending', 'active', 'failed')",
            name="ck_youtube_reach_job_status",
        ),
        sa.ForeignKeyConstraint(["youtube_connection_id"], ["youtube_connections.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "youtube_connection_id",
            "report_type_id",
            name="uq_youtube_reach_job_connection_type",
        ),
        sa.UniqueConstraint("provider_job_id"),
    )
    for column in ("youtube_connection_id", "provider_job_id", "status", "created_at"):
        op.create_index(
            f"ix_youtube_reach_reporting_jobs_{column}",
            "youtube_reach_reporting_jobs",
            [column],
        )

    op.create_table(
        "youtube_reach_report_imports",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("reporting_job_id", sa.Uuid(), nullable=False),
        sa.Column("youtube_connection_id", sa.Uuid(), nullable=False),
        sa.Column("provider_report_id", sa.String(length=255), nullable=False),
        sa.Column("report_start_time", sa.DateTime(timezone=True), nullable=True),
        sa.Column("report_end_time", sa.DateTime(timezone=True), nullable=True),
        sa.Column("provider_create_time", sa.DateTime(timezone=True), nullable=True),
        sa.Column("payload_sha256", sa.String(length=64), nullable=True),
        sa.Column("row_count", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("report_metadata", sa.JSON(), nullable=False),
        sa.Column("imported_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "status IN ('imported', 'failed')",
            name="ck_youtube_reach_report_import_status",
        ),
        sa.CheckConstraint(
            "row_count >= 0",
            name="ck_youtube_reach_report_row_count_nonnegative",
        ),
        sa.ForeignKeyConstraint(["reporting_job_id"], ["youtube_reach_reporting_jobs.id"]),
        sa.ForeignKeyConstraint(["youtube_connection_id"], ["youtube_connections.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "youtube_connection_id",
            "provider_report_id",
            name="uq_youtube_reach_report_connection_report",
        ),
    )
    for column in (
        "reporting_job_id",
        "youtube_connection_id",
        "provider_report_id",
        "provider_create_time",
        "status",
        "imported_at",
    ):
        op.create_index(
            f"ix_youtube_reach_report_imports_{column}",
            "youtube_reach_report_imports",
            [column],
        )

    op.create_table(
        "publication_reach_observations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("report_import_id", sa.Uuid(), nullable=False),
        sa.Column("publication_id", sa.Uuid(), nullable=False),
        sa.Column("packaging_variant_id", sa.Uuid(), nullable=True),
        sa.Column("youtube_video_id", sa.String(length=64), nullable=False),
        sa.Column("report_date", sa.Date(), nullable=False),
        sa.Column("thumbnail_impressions", sa.BigInteger(), nullable=True),
        sa.Column("thumbnail_ctr", sa.Numeric(12, 8), nullable=True),
        sa.Column("attribution_status", sa.String(length=32), nullable=False),
        sa.Column("raw_row", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "thumbnail_impressions IS NULL OR thumbnail_impressions >= 0",
            name="ck_publication_reach_impressions_nonnegative",
        ),
        sa.CheckConstraint(
            "thumbnail_ctr IS NULL OR (thumbnail_ctr >= 0 AND thumbnail_ctr <= 1)",
            name="ck_publication_reach_ctr_fraction",
        ),
        sa.CheckConstraint(
            "attribution_status IN ('variant', 'legacy_baseline', 'mixed_variant_day')",
            name="ck_publication_reach_attribution_status",
        ),
        sa.CheckConstraint(
            "(attribution_status = 'variant' AND packaging_variant_id IS NOT NULL) OR "
            "(attribution_status != 'variant' AND packaging_variant_id IS NULL)",
            name="ck_publication_reach_variant_attribution",
        ),
        sa.ForeignKeyConstraint(["report_import_id"], ["youtube_reach_report_imports.id"]),
        sa.ForeignKeyConstraint(["publication_id"], ["publications.id"]),
        sa.ForeignKeyConstraint(["packaging_variant_id"], ["publication_packaging_variants.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "report_import_id",
            "publication_id",
            "report_date",
            name="uq_publication_reach_import_publication_date",
        ),
    )
    for column in (
        "report_import_id",
        "publication_id",
        "packaging_variant_id",
        "youtube_video_id",
        "report_date",
        "attribution_status",
        "created_at",
    ):
        op.create_index(
            f"ix_publication_reach_observations_{column}",
            "publication_reach_observations",
            [column],
        )


def downgrade() -> None:
    op.drop_table("publication_reach_observations")
    op.drop_table("youtube_reach_report_imports")
    op.drop_table("youtube_reach_reporting_jobs")
