"""add durable interim intelligence ingestion

Revision ID: 0044_interim_intelligence_ingestion
Revises: 0043_chatgpt_plan_connections
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0044_interim_intelligence_ingestion"
down_revision: str | None = "0043_chatgpt_plan_connections"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "intelligence_ingest_batches",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("channel_profile_id", sa.Uuid(), nullable=False),
        sa.Column("batch_key", sa.String(length=160), nullable=False),
        sa.Column("producer", sa.String(length=128), nullable=False),
        sa.Column("source_type", sa.String(length=64), nullable=False),
        sa.Column("content_sha256", sa.String(length=64), nullable=False),
        sa.Column("record_count", sa.Integer(), nullable=False),
        sa.Column("batch_metadata", sa.JSON(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["channel_profile_id"], ["channel_profiles.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "channel_profile_id",
            "batch_key",
            name="uq_intelligence_ingest_batch_channel_key",
        ),
    )
    for column in (
        "channel_profile_id",
        "batch_key",
        "producer",
        "source_type",
        "content_sha256",
        "created_at",
    ):
        op.create_index(
            f"ix_intelligence_ingest_batches_{column}",
            "intelligence_ingest_batches",
            [column],
        )

    op.create_table(
        "intelligence_records",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("channel_profile_id", sa.Uuid(), nullable=False),
        sa.Column("first_batch_id", sa.Uuid(), nullable=False),
        sa.Column("last_batch_id", sa.Uuid(), nullable=False),
        sa.Column("record_kind", sa.String(length=64), nullable=False),
        sa.Column("record_key", sa.String(length=255), nullable=False),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("source_url", sa.Text(), nullable=True),
        sa.Column("platform", sa.String(length=32), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("tags", sa.JSON(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("provenance", sa.JSON(), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("event_time", sa.DateTime(timezone=True), nullable=True),
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
        sa.ForeignKeyConstraint(["channel_profile_id"], ["channel_profiles.id"]),
        sa.ForeignKeyConstraint(["first_batch_id"], ["intelligence_ingest_batches.id"]),
        sa.ForeignKeyConstraint(["last_batch_id"], ["intelligence_ingest_batches.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "channel_profile_id",
            "record_kind",
            "record_key",
            name="uq_intelligence_record_channel_kind_key",
        ),
    )
    for column in (
        "channel_profile_id",
        "first_batch_id",
        "last_batch_id",
        "record_kind",
        "record_key",
        "platform",
        "status",
        "observed_at",
        "event_time",
        "created_at",
    ):
        op.create_index(
            f"ix_intelligence_records_{column}",
            "intelligence_records",
            [column],
        )

    op.create_table(
        "intelligence_batch_records",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("batch_id", sa.Uuid(), nullable=False),
        sa.Column("record_id", sa.Uuid(), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("action", sa.String(length=32), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["batch_id"], ["intelligence_ingest_batches.id"]),
        sa.ForeignKeyConstraint(["record_id"], ["intelligence_records.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "batch_id",
            "record_id",
            name="uq_intelligence_batch_record_membership",
        ),
        sa.UniqueConstraint(
            "batch_id",
            "ordinal",
            name="uq_intelligence_batch_record_ordinal",
        ),
    )
    op.create_index(
        "ix_intelligence_batch_records_batch_id",
        "intelligence_batch_records",
        ["batch_id"],
    )
    op.create_index(
        "ix_intelligence_batch_records_record_id",
        "intelligence_batch_records",
        ["record_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_intelligence_batch_records_record_id",
        table_name="intelligence_batch_records",
    )
    op.drop_index(
        "ix_intelligence_batch_records_batch_id",
        table_name="intelligence_batch_records",
    )
    op.drop_table("intelligence_batch_records")

    for column in reversed(
        (
            "channel_profile_id",
            "first_batch_id",
            "last_batch_id",
            "record_kind",
            "record_key",
            "platform",
            "status",
            "observed_at",
            "event_time",
            "created_at",
        )
    ):
        op.drop_index(
            f"ix_intelligence_records_{column}",
            table_name="intelligence_records",
        )
    op.drop_table("intelligence_records")

    for column in reversed(
        (
            "channel_profile_id",
            "batch_key",
            "producer",
            "source_type",
            "content_sha256",
            "created_at",
        )
    ):
        op.drop_index(
            f"ix_intelligence_ingest_batches_{column}",
            table_name="intelligence_ingest_batches",
        )
    op.drop_table("intelligence_ingest_batches")
