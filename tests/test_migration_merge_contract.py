from alembic.config import Config
from alembic.script import ScriptDirectory


def test_ranked_trend_editing_activation_and_render_revisions_share_one_history() -> None:
    script = ScriptDirectory.from_config(Config("alembic.ini"))
    assert script.get_heads() == ["0034_brand_preview_renders"]

    previews = script.get_revision("0034_brand_preview_renders")
    assert previews.down_revision == "0033_channel_brand_control"

    brand_control = script.get_revision("0033_channel_brand_control")
    assert brand_control.down_revision == "0032_packaging_experiments"

    experiments = script.get_revision("0032_packaging_experiments")
    assert experiments.down_revision == "0031_packaging_candidate_generation"

    packaging_generation = script.get_revision("0031_packaging_candidate_generation")
    assert packaging_generation.down_revision == "0030_packaging_intelligence"

    packaging_intelligence = script.get_revision("0030_packaging_intelligence")
    assert packaging_intelligence.down_revision == "0029_packaging_reach"

    reach = script.get_revision("0029_packaging_reach")
    assert reach.down_revision == "0028_publication_packaging"

    packaging = script.get_revision("0028_publication_packaging")
    assert packaging.down_revision == "0027_edit_blueprint_performance"

    edit_performance = script.get_revision("0027_edit_blueprint_performance")
    assert edit_performance.down_revision == "0026_render_attempts"

    render_attempts = script.get_revision("0026_render_attempts")
    assert render_attempts.down_revision == "0025_trend_activation_performance"

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
