"""long-form compilation compiler

Revision ID: 0005_longform_compiler
Revises: 0004_youtube_publishing
Create Date: 2026-09-15
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005_longform_compiler"
down_revision: str | None = "0004_youtube_publishing"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "compilations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("parent_compilation_id", sa.Uuid(), nullable=True),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("regenerate_from", sa.String(length=32), nullable=True),
        sa.Column("workflow_id", sa.String(length=255), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("stage", sa.String(length=64), nullable=False),
        sa.Column("theme", sa.Text(), nullable=False),
        sa.Column("target_duration_seconds", sa.Integer(), nullable=False),
        sa.Column("target_segment_count", sa.Integer(), nullable=True),
        sa.Column("persona_key", sa.String(length=64), nullable=False),
        sa.Column("persona_version", sa.String(length=32), nullable=False),
        sa.Column("prompt_version", sa.String(length=32), nullable=False),
        sa.Column("candidate_snapshot", sa.JSON(), nullable=False),
        sa.Column("editor_plan", sa.JSON(), nullable=False),
        sa.Column("critic_feedback", sa.JSON(), nullable=False),
        sa.Column("final_plan", sa.JSON(), nullable=False),
        sa.Column("selected_voice_profile", sa.String(length=128), nullable=True),
        sa.Column("render_manifest", sa.JSON(), nullable=False),
        sa.Column("estimated_cost_usd", sa.Numeric(14, 8), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
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
        sa.ForeignKeyConstraint(["parent_compilation_id"], ["compilations.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("workflow_id"),
    )
    op.create_index(
        "ix_compilations_parent_compilation_id",
        "compilations",
        ["parent_compilation_id"],
    )
    op.create_index("ix_compilations_workflow_id", "compilations", ["workflow_id"])
    op.create_index("ix_compilations_status", "compilations", ["status"])

    op.create_table(
        "compilation_segments",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("compilation_id", sa.Uuid(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("clip_id", sa.Uuid(), nullable=False),
        sa.Column("short_production_id", sa.Uuid(), nullable=True),
        sa.Column("short_publication_id", sa.Uuid(), nullable=True),
        sa.Column("deterministic_score", sa.Numeric(8, 6), nullable=False),
        sa.Column("opening_score", sa.Numeric(8, 6), nullable=False),
        sa.Column("source_duration_seconds", sa.Numeric(12, 3), nullable=False),
        sa.Column("source_start_seconds", sa.Numeric(12, 3), nullable=False),
        sa.Column("source_end_seconds", sa.Numeric(12, 3), nullable=True),
        sa.Column("transition_before", sa.Text(), nullable=True),
        sa.Column("host_before", sa.Text(), nullable=True),
        sa.Column("host_after", sa.Text(), nullable=True),
        sa.Column("selection_reason", sa.Text(), nullable=True),
        sa.Column("evidence", sa.JSON(), nullable=False),
        sa.Column("timing", sa.JSON(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["clip_id"], ["clips.id"]),
        sa.ForeignKeyConstraint(["compilation_id"], ["compilations.id"]),
        sa.ForeignKeyConstraint(["short_production_id"], ["productions.id"]),
        sa.ForeignKeyConstraint(["short_publication_id"], ["publications.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("compilation_id", "position"),
        sa.UniqueConstraint("compilation_id", "clip_id"),
    )
    op.create_index(
        "ix_compilation_segments_compilation_id",
        "compilation_segments",
        ["compilation_id"],
    )
    op.create_index("ix_compilation_segments_clip_id", "compilation_segments", ["clip_id"])
    op.create_index(
        "ix_compilation_segments_short_production_id",
        "compilation_segments",
        ["short_production_id"],
    )
    op.create_index(
        "ix_compilation_segments_short_publication_id",
        "compilation_segments",
        ["short_publication_id"],
    )

    op.create_table(
        "compilation_assets",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("compilation_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(length=64), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("storage_key", sa.Text(), nullable=False),
        sa.Column("content_type", sa.String(length=128), nullable=True),
        sa.Column("provider", sa.String(length=32), nullable=True),
        sa.Column("model", sa.String(length=128), nullable=True),
        sa.Column("asset_metadata", sa.JSON(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["compilation_id"], ["compilations.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("compilation_id", "kind", "generation"),
    )
    op.create_index(
        "ix_compilation_assets_compilation_id",
        "compilation_assets",
        ["compilation_id"],
    )
    op.create_index("ix_compilation_assets_kind", "compilation_assets", ["kind"])

    op.create_table(
        "compilation_reviews",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("compilation_id", sa.Uuid(), nullable=False),
        sa.Column("decision", sa.String(length=32), nullable=False),
        sa.Column("actor", sa.String(length=128), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("review_metadata", sa.JSON(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["compilation_id"], ["compilations.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_compilation_reviews_compilation_id",
        "compilation_reviews",
        ["compilation_id"],
    )
    op.create_index(
        "ix_compilation_reviews_decision",
        "compilation_reviews",
        ["decision"],
    )


def downgrade() -> None:
    op.drop_index("ix_compilation_reviews_decision", table_name="compilation_reviews")
    op.drop_index(
        "ix_compilation_reviews_compilation_id",
        table_name="compilation_reviews",
    )
    op.drop_table("compilation_reviews")
    op.drop_index("ix_compilation_assets_kind", table_name="compilation_assets")
    op.drop_index(
        "ix_compilation_assets_compilation_id",
        table_name="compilation_assets",
    )
    op.drop_table("compilation_assets")
    op.drop_index(
        "ix_compilation_segments_short_publication_id",
        table_name="compilation_segments",
    )
    op.drop_index(
        "ix_compilation_segments_short_production_id",
        table_name="compilation_segments",
    )
    op.drop_index("ix_compilation_segments_clip_id", table_name="compilation_segments")
    op.drop_index(
        "ix_compilation_segments_compilation_id",
        table_name="compilation_segments",
    )
    op.drop_table("compilation_segments")
    op.drop_index("ix_compilations_status", table_name="compilations")
    op.drop_index("ix_compilations_workflow_id", table_name="compilations")
    op.drop_index(
        "ix_compilations_parent_compilation_id",
        table_name="compilations",
    )
    op.drop_table("compilations")
