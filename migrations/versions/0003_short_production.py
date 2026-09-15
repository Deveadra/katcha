"""short production ledger

Revision ID: 0003_short_production
Revises: 0002_media_intelligence
Create Date: 2026-09-15
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003_short_production"
down_revision: str | None = "0002_media_intelligence"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "productions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("clip_id", sa.Uuid(), nullable=False),
        sa.Column("workflow_id", sa.String(length=255), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("stage", sa.String(length=64), nullable=False),
        sa.Column("persona_key", sa.String(length=64), nullable=False),
        sa.Column("persona_version", sa.String(length=32), nullable=False),
        sa.Column("prompt_version", sa.String(length=32), nullable=False),
        sa.Column("analysis_snapshot", sa.JSON(), nullable=False),
        sa.Column("selected_script_id", sa.Uuid(), nullable=True),
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
        sa.ForeignKeyConstraint(["clip_id"], ["clips.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("workflow_id"),
    )
    op.create_index("ix_productions_clip_id", "productions", ["clip_id"])
    op.create_index("ix_productions_kind", "productions", ["kind"])
    op.create_index("ix_productions_status", "productions", ["status"])
    op.create_index("ix_productions_workflow_id", "productions", ["workflow_id"])

    op.create_table(
        "production_scripts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("production_id", sa.Uuid(), nullable=False),
        sa.Column("candidate_index", sa.Integer(), nullable=False),
        sa.Column("style", sa.String(length=64), nullable=False),
        sa.Column("narration", sa.Text(), nullable=False),
        sa.Column("interaction_prompt", sa.Text(), nullable=True),
        sa.Column("rationale", sa.Text(), nullable=True),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("model", sa.String(length=128), nullable=False),
        sa.Column("prompt_version", sa.String(length=32), nullable=False),
        sa.Column("selected", sa.Boolean(), nullable=False),
        sa.Column("script_metadata", sa.JSON(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["production_id"], ["productions.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("production_id", "candidate_index"),
    )
    op.create_index(
        "ix_production_scripts_production_id", "production_scripts", ["production_id"]
    )
    op.create_index("ix_production_scripts_selected", "production_scripts", ["selected"])

    op.create_table(
        "production_assets",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("production_id", sa.Uuid(), nullable=False),
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
        sa.ForeignKeyConstraint(["production_id"], ["productions.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("production_id", "kind", "generation"),
    )
    op.create_index("ix_production_assets_kind", "production_assets", ["kind"])
    op.create_index(
        "ix_production_assets_production_id", "production_assets", ["production_id"]
    )

    op.create_table(
        "production_reviews",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("production_id", sa.Uuid(), nullable=False),
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
        sa.ForeignKeyConstraint(["production_id"], ["productions.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_production_reviews_decision", "production_reviews", ["decision"])
    op.create_index(
        "ix_production_reviews_production_id", "production_reviews", ["production_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_production_reviews_production_id", table_name="production_reviews")
    op.drop_index("ix_production_reviews_decision", table_name="production_reviews")
    op.drop_table("production_reviews")
    op.drop_index("ix_production_assets_production_id", table_name="production_assets")
    op.drop_index("ix_production_assets_kind", table_name="production_assets")
    op.drop_table("production_assets")
    op.drop_index("ix_production_scripts_selected", table_name="production_scripts")
    op.drop_index("ix_production_scripts_production_id", table_name="production_scripts")
    op.drop_table("production_scripts")
    op.drop_index("ix_productions_workflow_id", table_name="productions")
    op.drop_index("ix_productions_status", table_name="productions")
    op.drop_index("ix_productions_kind", table_name="productions")
    op.drop_index("ix_productions_clip_id", table_name="productions")
    op.drop_table("productions")
