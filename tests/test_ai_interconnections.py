"""Real saved-data integration; fixtures do not prove live language comprehension."""

import json
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from katcha import db
from katcha.acquisition_models import IntelligenceRecord
from katcha.ai.command_planner import ClipLookup, CommandPlan
from katcha.brand_models import ChannelBrandVersion
from katcha.command_center_models import CommandActionProposal
from katcha.intelligence_models import ChannelProfile
from katcha.models import Clip, ClipFeature, DomainEvent, SourceItem
from katcha.services import command_center
from katcha.services.command_environment import command_environment
from katcha.services.command_planning import validate_bound_plan, workflow_observations
from katcha.services.command_resources import research_context
from katcha.trend_models import TrendOpportunity, TrendTopic


@pytest.fixture
def saved_data(monkeypatch):
    db.load_model_metadata()
    engine = create_engine("sqlite://")
    db.Base.metadata.create_all(engine)
    monkeypatch.setattr(db, "SessionLocal", sessionmaker(bind=engine, expire_on_commit=False))
    channels = [uuid.uuid4(), uuid.uuid4()]
    with db.session_scope() as session:
        for channel in channels:
            session.add(ChannelProfile(
                id=channel, youtube_connection_id=uuid.uuid4(), status="active",
                timezone="America/Chicago", profile_metadata={
                    "channel_title": "RankSnaxx", "channel_handle": "@fixture",
                    "secret_fixture": "must not enter planner context",
                },
            ))
    monkeypatch.setattr(
        command_center, "score_clip_for_channel", lambda *args: {"score": 0.8},
    )
    yield channels
    engine.dispose()


def _clip(channel, title, discovered):
    clip_id = uuid.uuid4()
    with db.session_scope() as session:
        session.add(Clip(id=clip_id, sha256=uuid.uuid4().hex * 2, storage_key="fixture.mp4"))
        session.add(ClipFeature(clip_id=clip_id, candidate_score=Decimal("80")))
        session.add(SourceItem(
            clip_id=clip_id, source_url=f"https://example.test/{clip_id}",
            canonical_url=f"https://example.test/{clip_id}", platform="youtube",
            title=title, discovered_at=discovered,
            source_metadata={"channel_profile_id": str(channel)},
        ))
    return clip_id


def test_planner_snapshot_is_read_only_and_omits_arbitrary_metadata(saved_data):
    context = command_environment(saved_data[0])
    assert context["channel"]["name"] == "RankSnaxx"
    assert context["channel"]["timezone"] == "America/Chicago"
    assert context["constraints"]["ranked_item_counts"] == [3, 5, 7]
    assert "secret_fixture" not in json.dumps(context)
    with db.session_scope() as session:
        assert session.scalar(select(ChannelBrandVersion)) is None


def test_semantic_retrieval_ignores_filler_and_unstated_date_and_other_channel(saved_data):
    old = datetime.now(UTC) - timedelta(days=10)
    expected = _clip(saved_data[0], "Xbox amazing comeback", old)
    _clip(saved_data[1], "Xbox amazing comeback", datetime.now(UTC))
    _, evidence = command_center.best_clips(
        saved_data[0], "Could you dig up something we could build around?",
        lookup=ClipLookup(terms=["xbox"]),
    )
    assert [row["id"] for row in evidence] == [str(expected)]
    assert evidence[0]["time_window"]["start"] is None
    _, today = command_center.best_clips(
        saved_data[0], "Earlier today", lookup=ClipLookup(terms=["xbox"], period="today"),
    )
    assert today == []


def test_retrieval_honors_candidate_count_and_alternative_topics(saved_data):
    for index in range(7):
        _clip(saved_data[0], "Xbox" if index % 2 else "PlayStation", datetime.now(UTC))
    _, candidates = command_center.best_clips(
        saved_data[0], "A ranking with seven clips",
        lookup=ClipLookup(terms=["xbox", "playstation"], limit=7),
    )
    assert len(candidates) == 7
    _, both = command_center.best_clips(
        saved_data[0], "A direct comparison",
        lookup=ClipLookup(terms=["xbox", "playstation"], match="all"),
    )
    assert both == []


def test_retained_research_and_current_trends_are_channel_scoped(saved_data):
    now = datetime.now(UTC)
    batch = uuid.uuid4()
    with db.session_scope() as session:
        for channel, status, title in [
            (saved_data[0], "active", "VisionQuest research"),
            (saved_data[1], "active", "Other channel research"),
            (saved_data[0], "archived", "Archived research"),
        ]:
            session.add(IntelligenceRecord(
                channel_profile_id=channel, first_batch_id=batch, last_batch_id=batch,
                record_kind="research", record_key=title, title=title,
                summary="Official announcements", status=status, observed_at=now,
            ))
        topic = TrendTopic(
            id=uuid.uuid4(), topic_key="fixture-topic", display_name="VisionQuest",
            first_seen_at=now, last_seen_at=now,
        )
        session.add(topic)
        for channel, hours in [(saved_data[0], 1), (saved_data[0], -1), (saved_data[1], 1)]:
            session.add(TrendOpportunity(
                channel_profile_id=channel, trend_topic_id=topic.id, watch_version=1,
                run_key=str(uuid.uuid4()), lifecycle="rising", opportunity_score=Decimal("0.8"),
                confidence=Decimal("0.9"), expires_at=now + timedelta(hours=hours),
            ))
    _, evidence = research_context(saved_data[0], ["visionquest"])
    assert [row["kind"] for row in evidence] == ["intelligence_record", "trend_opportunity"]
    assert evidence[0]["title"] == "VisionQuest research"
    _, unrelated = research_context(saved_data[0], ["unrelated"])
    assert unrelated == []


def test_fresh_workflow_feedback_reads_terminal_event_instead_of_startup(saved_data):
    proposal = CommandActionProposal(
        id=uuid.uuid4(), request_id=uuid.uuid4(), channel_profile_id=saved_data[0],
        action_type="start_source_scout", label="Search", description="Fixture search",
        status="executed", idempotency_key=str(uuid.uuid4()),
        expires_at=datetime.now(UTC) + timedelta(hours=1), result={"workflow_id": "fixture"},
    )
    with db.session_scope() as session:
        session.add(proposal)
    assert workflow_observations([proposal], saved_data[0])[0]["state"] == "workflow_started"
    with db.session_scope() as session:
        session.add(DomainEvent(
            aggregate_type="command_action_proposal", aggregate_id=str(proposal.id),
            event_type="command_center.workflow_failed",
            payload={"workflow_id": "fixture", "detail": {"error": "No matching trailers"}},
        ))
    result = workflow_observations([proposal], saved_data[0])[0]
    assert result["state"] == "failed"
    assert result["settled"] is True
    assert result["detail"]["error"] == "No matching trailers"
    with pytest.raises(ValueError, match="different channel"):
        workflow_observations([proposal], saved_data[1])


@pytest.mark.parametrize("update", [
    {"requested_actions": ["start_source_scout", "create_short_production"]},
    {"execution": "run"}, {"recurring": True}, {"prepare_for_production": True},
])
def test_observation_cannot_expand_original_authority(update):
    initial = CommandPlan(
        intent="source_discovery", confidence=0.99, reason="Preview a search",
        requested_actions=["start_source_scout"], execution="propose",
    )
    with pytest.raises(ValueError):
        validate_bound_plan(initial, initial.model_copy(update=update))
