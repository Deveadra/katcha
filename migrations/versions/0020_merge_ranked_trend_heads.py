"""Merge independently landed ranked-episode and trend-reliability revisions.

Revision ID: 0020_merge_ranked_trend_heads
Revises: 0019_ranked_episode_render_publishing, 0019_trend_discovery_reliability
"""

from collections.abc import Sequence

revision: str = "0020_merge_ranked_trend_heads"
down_revision: tuple[str, str] = (
    "0019_ranked_episode_render_publishing",
    "0019_trend_discovery_reliability",
)
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Both parent migrations own their DDL; the merge only joins history."""


def downgrade() -> None:
    """Preserve both histories when stepping back to the two parent heads."""
