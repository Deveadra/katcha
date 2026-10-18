from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

from katcha.db import session_scope
from katcha.services.trend_auto_activation import _episode_for_same_topic_and_media
from katcha.short_episode_models import ShortEpisode, ShortEpisodeItem
from katcha.trend_models import TrendOpportunity, TrendTopic


def test_rescored_topic_reuses_identical_media_without_blocking_new_evidence(
    fixture_work,
):
    clip_id, channel, other_channel, _ = fixture_work
    now = datetime.now(UTC)
    with session_scope() as session:
        topic = TrendTopic(
            topic_key="fixture-topic", display_name="Fixture film update",
            first_seen_at=now, last_seen_at=now,
        )
        session.add(topic)
        session.flush()

        def opportunity(profile_id, key):
            row = TrendOpportunity(
                channel_profile_id=profile_id,
                trend_topic_id=topic.id,
                watch_version=1, run_key=key, lifecycle="rising",
                opportunity_score=Decimal("0.80"),
                confidence=Decimal("0.70"),
                expires_at=now + timedelta(hours=24),
            )
            session.add(row)
            session.flush()
            return row

        prior_opportunity = opportunity(channel, "scheduled-first")
        rescored = opportunity(channel, "scheduled-next")
        other = opportunity(other_channel, "scheduled-other")
        old_episode = ShortEpisode(
            channel_profile_id=channel,
            trend_opportunity_id=prior_opportunity.id,
            workflow_id="fixture-trend-original",
            status="voiced", stage="voiced",
            premise="First report", format_key="countdown",
            format_version="1", item_count=1,
            persona_key="fixture", persona_version="1",
            brand_key="fixture", brand_version=1,
        )
        session.add(old_episode)
        session.flush()
        session.add(ShortEpisodeItem(
            short_episode_id=old_episode.id,
            clip_id=clip_id, position=1, role="hook",
            overall_score=Decimal("0.9"),
            role_score=Decimal("0.8"),
        ))
        session.flush()

        repeated = _episode_for_same_topic_and_media(
            session, channel, rescored,
            format_key="countdown", clip_ids=(clip_id,),
        )
        assert repeated is not None
        assert repeated.id == old_episode.id

        # A legitimate new source/clip or editorial format remains eligible.
        assert _episode_for_same_topic_and_media(
            session, channel, rescored,
            format_key="countdown", clip_ids=(uuid4(),),
        ) is None
        assert _episode_for_same_topic_and_media(
            session, channel, rescored,
            format_key="different-format", clip_ids=(clip_id,),
        ) is None
        # A second channel cannot inherit the first channel's duplicate gate.
        assert _episode_for_same_topic_and_media(
            session, other_channel, other,
            format_key="countdown", clip_ids=(clip_id,),
        ) is None

        # The same opportunity retains its original restart/recovery path.
        assert _episode_for_same_topic_and_media(
            session, channel, prior_opportunity,
            format_key="countdown", clip_ids=(clip_id,),
        ) is None

        old_episode.status = "failed"
        session.flush()
        assert _episode_for_same_topic_and_media(
            session, channel, rescored,
            format_key="countdown", clip_ids=(clip_id,),
        ) is None
