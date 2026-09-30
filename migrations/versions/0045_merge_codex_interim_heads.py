"""merge Codex and interim intelligence migration heads

Revision ID: 0045_merge_codex_interim_heads
Revises: 0044_codex_plan_connections, 0044_interim_intelligence_ingestion
"""

from collections.abc import Sequence

revision: str = "0045_merge_codex_interim_heads"
down_revision: tuple[str, str] = (
    "0044_codex_plan_connections",
    "0044_interim_intelligence_ingestion",
)
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
