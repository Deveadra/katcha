from alembic.config import Config
from alembic.script import ScriptDirectory


def test_ranked_trend_editing_and_activation_revisions_share_one_history() -> None:
    script = ScriptDirectory.from_config(Config("alembic.ini"))
    assert script.get_heads() == ["0025_trend_activation_performance"]

    performance = script.get_revision("0025_trend_activation_performance")
    assert performance.down_revision == "0024_channel_edit_blueprints"

    editing = script.get_revision("0024_channel_edit_blueprints")
    assert editing.down_revision == "0023_autonomous_trend_activation"

    activation = script.get_revision("0023_autonomous_trend_activation")
    assert activation.down_revision == "0022_discovery_poll_quota"

    quota = script.get_revision("0022_discovery_poll_quota")
    assert quota.down_revision == "0021_trend_calibration"

    calibration = script.get_revision("0021_trend_calibration")
    assert calibration.down_revision == "0020_merge_ranked_trend_heads"

    merged = script.get_revision("0020_merge_ranked_trend_heads")
    assert set(merged.down_revision) == {
        "0019_ranked_episode_render_publishing",
        "0019_trend_discovery_reliability",
    }
