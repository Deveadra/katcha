from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest

from katcha import db
from katcha.intelligence_models import ChannelEconomicsSnapshot
from katcha.services import trend_editorial_dispatch as dispatcher
from katcha.services.trend_auto_activation import create_activation_policy
from katcha.short_episode_models import ShortEpisode, ShortEpisodeItem


def _policy(enabled=True, auto_start=False):
    return SimpleNamespace(
        enabled=enabled,
        policy_metadata={"auto_start_editorial": auto_start},
        min_budget_headroom_usd=Decimal("2.00"),
    )


def test_editorial_paid_dispatch_requires_explicit_permission_and_recent_budget():
    now = timedelta(minutes=10)
    ready = dict(
        ai_enabled=True,
        provider_ready=True,
        economics_age=now,
        headroom=Decimal("3"),
    )
    assert dispatcher._editorial_dispatch_readiness(_policy(), **ready) == (
        "editorial_auto_start_not_authorized"
    )
    assert dispatcher._editorial_dispatch_readiness(
        _policy(auto_start=True),
        **{**ready, "economics_age": timedelta(days=3)},
    ) == "economics_snapshot_stale"
    assert dispatcher._editorial_dispatch_readiness(
        _policy(auto_start=True),
        **{**ready, "headroom": Decimal("1")},
    ) == "insufficient_budget_headroom"
    assert dispatcher._editorial_dispatch_readiness(
        _policy(auto_start=True),
        **{**ready, "provider_ready": False},
    ) == "ai_provider_unavailable"
    assert dispatcher._editorial_dispatch_readiness(
        _policy(auto_start=True), **ready
    ) is None


def test_editorial_dispatch_rejects_unbounded_or_boolean_batch_policy():
    policy = _policy(auto_start=True)
    policy.policy_metadata["editorial_max_starts_per_scan"] = 100
    assert dispatcher._safe_limit(policy) == 1
    policy.policy_metadata["editorial_max_starts_per_scan"] = True
    assert dispatcher._safe_limit(policy) == 1
    policy.policy_metadata["editorial_max_starts_per_scan"] = 2
    assert dispatcher._safe_limit(policy) == 2


@pytest.mark.asyncio
async def test_planned_trend_episode_starts_existing_workflow_without_manual_handoff(
    fixture_work, monkeypatch,
):
    clip_id, channel, other, ids = fixture_work
    now = datetime.now(UTC)
    create_activation_policy(
        channel,
        enabled=True,
        metadata={"auto_start_editorial": True},
    )
    with db.session_scope() as session:
        episode = session.get(ShortEpisode, ids["short_episode"])
        episode.trend_opportunity_id = uuid.uuid4()
        items = list(session.query(ShortEpisodeItem).filter_by(short_episode_id=episode.id))
        assert len(items) == 1
        items[0].acquisition_snapshot = {"eligible": True}
        session.add(
            ChannelEconomicsSnapshot(
                channel_profile_id=channel,
                sample_key="editorial-test",
                sampled_at=now - timedelta(hours=1),
                budget_headroom_usd=Decimal("4"),
            )
        )

    monkeypatch.setattr(
        dispatcher, "get_settings",
        lambda: SimpleNamespace(
            ai_enabled=True, openai_api_key="fixture-only",
            gemini_api_key=None, env="test",
            resolved_ai_execution_mode=lambda: "fixture",
        ),
    )
    validated = []
    launched = []
    monkeypatch.setattr(
        dispatcher, "assert_clip_production_eligible",
        lambda clip: validated.append(clip),
    )
    async def start(episode_id, workflow_id, *, start_stage):
        launched.append((episode_id, workflow_id, start_stage))
        return workflow_id
    monkeypatch.setattr(dispatcher, "start_short_episode_editorial_workflow", start)

    result = await dispatcher.dispatch_planned_trend_editorial(channel, now=now)
    assert result["status"] == "dispatched"
    assert len(result["started"]) == 1
    assert validated == [clip_id]
    assert launched == [
        (str(ids["short_episode"]), "purge-episode-editorial-script", "script")
    ]
    other_result = await dispatcher.dispatch_planned_trend_editorial(other, now=now)
    assert other_result["status"] == "blocked"


@pytest.mark.asyncio
async def test_editorial_dispatch_pauses_without_permission_or_active_ai(
    fixture_work, monkeypatch,
):
    _, channel, _, _ = fixture_work
    create_activation_policy(channel, enabled=True)
    monkeypatch.setattr(
        dispatcher, "get_settings",
        lambda: SimpleNamespace(
            ai_enabled=False, openai_api_key=None, gemini_api_key=None,
            env="test", resolved_ai_execution_mode=lambda: "fixture",
        ),
    )
    monkeypatch.setattr(dispatcher, "subscription_connected", lambda _: False)
    async def must_not_dispatch(*args, **kwargs):
        raise AssertionError("no provider call allowed")
    monkeypatch.setattr(
        dispatcher, "start_short_episode_editorial_workflow", must_not_dispatch
    )
    summary = await dispatcher.dispatch_planned_trend_editorial(channel)
    assert summary["status"] == "blocked"
    assert summary["reason"] == "editorial_auto_start_not_authorized"


@pytest.mark.asyncio
async def test_fresh_eligible_episode_is_not_starved_by_old_blocked_plans(
    fixture_work, monkeypatch,
):
    clip_id, channel, other, ids = fixture_work
    now = datetime.now(UTC)
    create_activation_policy(
        channel,
        enabled=True,
        metadata={"auto_start_editorial": True},
    )
    with db.session_scope() as session:
        fresh = session.get(ShortEpisode, ids["short_episode"])
        fresh.trend_opportunity_id = uuid.uuid4()
        fresh.created_at = now
        for item in session.query(ShortEpisodeItem).filter_by(short_episode_id=fresh.id):
            item.acquisition_snapshot = {"eligible": True}

        # Every old row is still "planned", but has no production-ready media.
        # The previous oldest-first / 10-row window never reached the fresh
        # episode. This must not block new, qualified editorial production.
        for index in range(15):
            session.add(
                ShortEpisode(
                    channel_profile_id=channel,
                    trend_opportunity_id=uuid.uuid4(),
                    workflow_id=f"stale-unready-episode-{index}",
                    status="planned",
                    stage="planned",
                    premise="Older blocked footage",
                    format_key="countdown",
                    format_version="1",
                    item_count=1,
                    persona_key="fixture",
                    persona_version="1",
                    brand_key="fixture",
                    brand_version=1,
                    created_at=now - timedelta(days=index + 1),
                )
            )
        session.add(
            ChannelEconomicsSnapshot(
                channel_profile_id=channel,
                sample_key="fresh-editorial-dispatch",
                sampled_at=now - timedelta(minutes=5),
                budget_headroom_usd=Decimal("4"),
            )
        )
    monkeypatch.setattr(
        dispatcher, "get_settings",
        lambda: SimpleNamespace(
            ai_enabled=True,
            openai_api_key="fixture-only",
            gemini_api_key=None,
            env="test",
            resolved_ai_execution_mode=lambda: "fixture",
        ),
    )
    validated = []
    launched = []
    monkeypatch.setattr(
        dispatcher, "assert_clip_production_eligible",
        lambda clip: validated.append(clip),
    )

    async def start(episode_id, workflow_id, *, start_stage):
        launched.append((episode_id, workflow_id, start_stage))
        return workflow_id

    monkeypatch.setattr(dispatcher, "start_short_episode_editorial_workflow", start)

    result = await dispatcher.dispatch_planned_trend_editorial(channel, now=now)
    assert result["status"] == "dispatched"
    assert len(result["started"]) == 1
    assert result["started"][0]["episode_id"] == str(ids["short_episode"])
    assert validated == [clip_id]
    assert launched == [
        (str(ids["short_episode"]), "purge-episode-editorial-script", "script")
    ]
    assert result["failed"] == []
    assert await dispatcher.dispatch_planned_trend_editorial(other, now=now) == {
        "status": "blocked",
        "reason": "activation_policy_disabled",
        "started": [],
        "failed": [],
    }
