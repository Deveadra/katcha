"""add durable project media bin references

Revision ID: 0058_project_media_bin
Revises: 0057_editorial_image_lineage
Create Date: 2026-10-11
"""

import sqlalchemy as sa
from alembic import op

revision = "0058_project_media_bin"
down_revision = "0057_editorial_image_lineage"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_unique_constraint(
        "uq_editorial_project_channel_scope",
        "editorial_projects",
        ["id", "channel_profile_id"],
    )
    op.create_table(
        "project_media_items",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("channel_profile_id", sa.Uuid(), nullable=False),
        sa.Column("clip_id", sa.Uuid(), nullable=True),
        sa.Column("external_reference_url", sa.Text(), nullable=True),
        sa.Column("request_id", sa.Uuid(), nullable=False),
        sa.Column("request_digest", sa.String(64), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("rights_status", sa.String(32), nullable=False),
        sa.Column("rights_assessment_id", sa.Uuid(), nullable=True),
        sa.Column("rights_assessment_version", sa.Integer(), nullable=True),
        sa.Column("requirement_ids", sa.JSON(), nullable=False),
        sa.Column("searchable_tags", sa.JSON(), nullable=False),
        sa.Column("provenance", sa.JSON(), nullable=False),
        sa.Column("source_metadata", sa.JSON(), nullable=False),
        sa.Column("source_in_seconds", sa.Float(), nullable=True),
        sa.Column("source_out_seconds", sa.Float(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=False),
        sa.Column("actor", sa.String(255), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("last_verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("revision > 0", name="ck_project_media_item_revision"),
        sa.CheckConstraint(
            "(clip_id IS NOT NULL AND external_reference_url IS NULL) OR "
            "(clip_id IS NULL AND external_reference_url IS NOT NULL)",
            name="ck_project_media_item_single_source",
        ),
        sa.CheckConstraint(
            "status IN ('reference_only', 'review_required', 'denied', 'acquired', 'ready')",
            name="ck_project_media_item_status",
        ),
        sa.CheckConstraint(
            "rights_status IN ('not_assessed', 'reference_only', 'review_permitted', "
            "'production_cleared', 'restricted', 'denied', 'revoked', 'expired')",
            name="ck_project_media_item_rights_status",
        ),
        sa.CheckConstraint(
            "status != 'ready' OR (rights_status = 'production_cleared' AND clip_id IS NOT NULL)",
            name="ck_project_media_item_ready_requires_cleared_clip",
        ),
        sa.ForeignKeyConstraint(
            ["project_id", "channel_profile_id"],
            ["editorial_projects.id", "editorial_projects.channel_profile_id"],
            name="fk_project_media_item_project_channel",
        ),
        sa.ForeignKeyConstraint(
            ["project_id", "revision"],
            ["editorial_revisions.project_id", "editorial_revisions.revision"],
            name="fk_project_media_item_revision",
        ),
        sa.ForeignKeyConstraint(["channel_profile_id"], ["channel_profiles.id"]),
        sa.ForeignKeyConstraint(["clip_id"], ["clips.id"]),
        sa.ForeignKeyConstraint(["rights_assessment_id"], ["rights_assessments.id"]),
        sa.CheckConstraint(
            "(rights_assessment_id IS NULL AND rights_assessment_version IS NULL) OR "
            "(rights_assessment_id IS NOT NULL AND rights_assessment_version > 0)",
            name="ck_project_media_item_rights_assessment_version",
        ),
        sa.CheckConstraint(
            "rights_status != 'production_cleared' OR rights_assessment_id IS NOT NULL",
            name="ck_project_media_item_clearance_has_assessment",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("request_id", name="uq_project_media_item_request"),
    )
    for column in ("project_id", "channel_profile_id", "clip_id"):
        op.create_index(f"ix_project_media_items_{column}", "project_media_items", [column])


def downgrade() -> None:
    op.drop_table("project_media_items")
    op.drop_constraint(
        "uq_editorial_project_channel_scope", "editorial_projects", type_="unique"
    )
