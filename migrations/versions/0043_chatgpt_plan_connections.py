"""add ChatGPT plan OAuth connections

Revision ID: 0043_chatgpt_plan_connections
Revises: 0042_channel_provider_settings
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0043_chatgpt_plan_connections"
down_revision: str | None = "0042_channel_provider_settings"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "chatgpt_connections",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("issued_client_id", sa.String(length=255), nullable=False),
        sa.Column("issuer", sa.String(length=255), nullable=False),
        sa.Column("subject", sa.String(length=255), nullable=False),
        sa.Column("email", sa.Text(), nullable=True),
        sa.Column("display_name", sa.Text(), nullable=True),
        sa.Column("scopes", sa.JSON(), nullable=False),
        sa.Column("encrypted_access_token", sa.Text(), nullable=True),
        sa.Column("encrypted_refresh_token", sa.Text(), nullable=True),
        sa.Column("encrypted_id_token", sa.Text(), nullable=True),
        sa.Column("access_token_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("selected_model", sa.String(length=255), nullable=True),
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
        sa.UniqueConstraint("issued_client_id"),
    )
    op.create_index(
        "ix_chatgpt_connections_issued_client_id",
        "chatgpt_connections",
        ["issued_client_id"],
        unique=True,
    )
    op.create_index(
        "ix_chatgpt_connections_subject",
        "chatgpt_connections",
        ["subject"],
    )
    op.create_index(
        "ix_chatgpt_connections_active",
        "chatgpt_connections",
        ["active"],
    )

    op.create_table(
        "chatgpt_oauth_states",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("state_hash", sa.String(length=64), nullable=False),
        sa.Column("nonce", sa.String(length=255), nullable=False),
        sa.Column("encrypted_code_verifier", sa.Text(), nullable=False),
        sa.Column("redirect_uri", sa.Text(), nullable=False),
        sa.Column("expected_client_id", sa.String(length=255), nullable=True),
        sa.Column("connection_id", sa.Uuid(), nullable=True),
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
        "ix_chatgpt_oauth_states_state_hash",
        "chatgpt_oauth_states",
        ["state_hash"],
        unique=True,
    )
    op.create_index(
        "ix_chatgpt_oauth_states_connection_id",
        "chatgpt_oauth_states",
        ["connection_id"],
    )
    op.create_index(
        "ix_chatgpt_oauth_states_expires_at",
        "chatgpt_oauth_states",
        ["expires_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_chatgpt_oauth_states_expires_at", table_name="chatgpt_oauth_states")
    op.drop_index("ix_chatgpt_oauth_states_connection_id", table_name="chatgpt_oauth_states")
    op.drop_index("ix_chatgpt_oauth_states_state_hash", table_name="chatgpt_oauth_states")
    op.drop_table("chatgpt_oauth_states")

    op.drop_index("ix_chatgpt_connections_active", table_name="chatgpt_connections")
    op.drop_index("ix_chatgpt_connections_subject", table_name="chatgpt_connections")
    op.drop_index("ix_chatgpt_connections_issued_client_id", table_name="chatgpt_connections")
    op.drop_table("chatgpt_connections")
