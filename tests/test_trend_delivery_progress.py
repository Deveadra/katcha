from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from katcha.api.trend_auto_activation import get_trend_delivery_progress
from katcha.db import session_scope
from katcha.short_episode_models import ShortEpisode, ShortEpisodeAsset, ShortEpisodeScript


def test_delivery_progress_distinguishes_manifests_from_verified_outputs(
    fixture_work,
):
    _, channel, other, ids = fixture_work
    with session_scope() as session:
        episode = session.get(ShortEpisode, ids["short_episode"])
        episode.trend_opportunity_id = uuid.uuid4()
        episode.status = "rendering"
        episode.stage = "render_manifest_ready"
        episode.render_manifest = {"output_key": "not-yet-produced.mp4"}
        script = ShortEpisodeScript(
            short_episode_id=episode.id,
            candidate_index=1,
            style="ranked",
            provider="fixture",
            model="fixture",
            prompt_version="v1",
            selected=True,
        )
        session.add(script)
        session.flush()
        episode.selected_script_id = script.id
        session.add_all([
            ShortEpisodeAsset(
                short_episode_id=episode.id, kind="narration_1",
                storage_key="private/narration.wav", content_type="audio/wav",
            ),
            ShortEpisodeAsset(
                short_episode_id=episode.id, kind="render", generation=1,
                storage_key="private/unverified.mp4", content_type="video/mp4",
                asset_metadata={"verified": False},
            ),
        ])
        foreign = ShortEpisode(
            channel_profile_id=other, trend_opportunity_id=uuid.uuid4(),
            workflow_id="other-episode-delivery", status="rendered",
            stage="render_verified", premise="Other channel", format_key="countdown",
            format_version="1", item_count=1, persona_key="fixture",
            persona_version="1", brand_key="fixture", brand_version=1,
        )
        session.add(foreign)
        session.flush()
        session.add(
            ShortEpisodeAsset(
                short_episode_id=foreign.id, kind="render",
                storage_key="private/other-verified.mp4",
                content_type="video/mp4",
                asset_metadata={"verified": True},
            )
        )
        foreign_id = foreign.id

    own = get_trend_delivery_progress(channel, limit=10)
    assert own.channel_profile_id == channel
    assert len(own.episodes) == 1
    item = own.episodes[0]
    assert item.episode_id == ids["short_episode"]
    assert item.script_selected is True
    assert item.narration_assets_recorded == 1
    assert item.render_manifest_recorded is True
    assert item.render_verified is False
    assert item.render_asset_id is None
    assert own.scripted_count == 1
    assert own.verified_render_count == 0
    assert own.stalled_count == 0
    assert item.health == "render_pending"

    separate = get_trend_delivery_progress(other, limit=10)
    assert len(separate.episodes) == 1
    assert separate.episodes[0].episode_id == foreign_id
    assert separate.verified_render_count == 1
    assert separate.episodes[0].render_verified is True
    assert separate.episodes[0].render_asset_id is not None


def test_delivery_progress_reports_overdue_and_failed_without_workflow_claims(
    fixture_work,
):
    _, channel, other, ids = fixture_work
    old = datetime.now(UTC) - timedelta(days=3)
    with session_scope() as session:
        episode = session.get(ShortEpisode, ids["short_episode"])
        episode.trend_opportunity_id = uuid.uuid4()
        episode.created_at = old
        episode.updated_at = old
        failed = ShortEpisode(
            channel_profile_id=channel, trend_opportunity_id=uuid.uuid4(),
            workflow_id="failed-trend-delivery", status="failed",
            stage="script_generation_failed", premise="Failed generation",
            format_key="countdown", format_version="1", item_count=1,
            persona_key="fixture", persona_version="1", brand_key="fixture",
            brand_version=1,
        )
        session.add(failed)
        session.flush()
        failed_id = failed.id

    response = get_trend_delivery_progress(channel, limit=10)
    by_id = {item.episode_id: item for item in response.episodes}
    assert by_id[ids["short_episode"]].health == "planning_overdue"
    assert by_id[ids["short_episode"]].next_step == (
        "editorial_dispatch_or_authorization"
    )
    assert by_id[failed_id].health == "failed"
    assert by_id[failed_id].next_step == "operator_attention"
    assert response.stalled_count == 2
    assert response.verified_render_count == 0
    assert get_trend_delivery_progress(other, limit=10).episodes == []
