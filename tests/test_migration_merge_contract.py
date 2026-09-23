from alembic.config import Config
from alembic.script import ScriptDirectory


def test_ranked_and_trend_reliability_revisions_share_one_head() -> None:
    script = ScriptDirectory.from_config(Config("alembic.ini"))
    assert script.get_heads() == ["0020_merge_ranked_trend_heads"]
    merged = script.get_revision("0020_merge_ranked_trend_heads")
    assert set(merged.down_revision) == {
        "0019_ranked_episode_render_publishing",
        "0019_trend_discovery_reliability",
    }
