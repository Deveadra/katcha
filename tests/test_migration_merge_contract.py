from alembic.config import Config
from alembic.script import ScriptDirectory


def test_ranked_trend_editing_activation_and_render_revisions_share_one_history() -> None:
    script = ScriptDirectory.from_config(Config("alembic.ini"))
    assert script.get_heads() == ["0046_command_goals"]
    assert script.get_revision("0046_command_goals").down_revision == "0045_merge_codex_interim_heads"

    merged_heads = script.get_revision("0045_merge_codex_interim_heads")
    assert set(merged_heads.down_revision) == {
        "0044_codex_plan_connections",
        "0044_interim_intelligence_ingestion",
    }

    codex_connections = script.get_revision("0044_codex_plan_connections")
    assert codex_connections.down_revision == "0043_chatgpt_plan_connections"

    interim_ingestion = script.get_revision("0044_interim_intelligence_ingestion")
    assert interim_ingestion.down_revision == "0043_chatgpt_plan_connections"

    chatgpt_connections = script.get_revision("0043_chatgpt_plan_connections")
    assert chatgpt_connections.down_revision == "0042_channel_provider_settings"

    provider_settings = script.get_revision("0042_channel_provider_settings")
    assert provider_settings.down_revision == "0041_external_edit_handoffs"

    external_edit = script.get_revision("0041_external_edit_handoffs")
    assert external_edit.down_revision == "0040_command_conversations"

    conversations = script.get_revision("0040_command_conversations")
    assert conversations.down_revision == "0039_telegram_reviews"

    telegram = script.get_revision("0039_telegram_reviews")
    assert telegram.down_revision == "0038_command_action_proposals"

    proposals = script.get_revision("0038_command_action_proposals")
    assert proposals.down_revision == "0037_clip_lifecycle"

    lifecycle = script.get_revision("0037_clip_lifecycle")
    assert lifecycle.down_revision == "0036_ingestion_sources"

    ingestion_sources = script.get_revision("0036_ingestion_sources")
    assert ingestion_sources.down_revision == "0035_ranked_brand_previews"

    ranked_previews = script.get_revision("0035_ranked_brand_previews")
    assert ranked_previews.down_revision == "0034_brand_preview_renders"

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
