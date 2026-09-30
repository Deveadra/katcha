"""add Codex plan OAuth connections

Revision ID: 0044_codex_plan_connections
Revises: 0043_chatgpt_plan_connections
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0044_codex_plan_connections"
down_revision: str | None = "0043_chatgpt_plan_connections"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "codex_connections",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("subject", sa.String(length=255), nullable=False),
        sa.Column("email", sa.Text(), nullable=True),
        sa.Column("account_id", sa.String(length=255), nullable=True),
        sa.Column("plan_type", sa.String(length=64), nullable=True),
        sa.Column("encrypted_access_token", sa.Text(), nullable=False),
        sa.Column("encrypted_refresh_token", sa.Text(), nullable=False),
        sa.Column("access_token_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("selected_model", sa.String(length=255), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("connection_metadata", sa.JSON(), nullable=False),
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
    )
    op.create_index(
        "ix_codex_connections_subject",
        "codex_connections",
        ["subject"],
    )
    op.create_index(
        "ix_codex_connections_account_id",
        "codex_connections",
        ["account_id"],
    )
    op.create_index(
        "ix_codex_connections_active",
        "codex_connections",
        ["active"],
    )

    op.create_table(
        "codex_oauth_states",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("state_hash", sa.String(length=64), nullable=False),
        sa.Column("encrypted_code_verifier", sa.Text(), nullable=False),
        sa.Column("redirect_uri", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("state_hash"),
    )
    op.create_index(
        "ix_codex_oauth_states_state_hash",
        "codex_oauth_states",
        ["state_hash"],
        unique=True,
    )
    op.create_index(
        "ix_codex_oauth_states_expires_at",
        "codex_oauth_states",
        ["expires_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_codex_oauth_states_expires_at", table_name="codex_oauth_states")
    op.drop_index("ix_codex_oauth_states_state_hash", table_name="codex_oauth_states")
    op.drop_table("codex_oauth_states")
    op.drop_index("ix_codex_connections_active", table_name="codex_connections")
    op.drop_index("ix_codex_connections_account_id", table_name="codex_connections")
    op.drop_index("ix_codex_connections_subject", table_name="codex_connections")
    op.drop_table("codex_connections")
