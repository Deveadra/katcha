from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import katcha.api.control_auth as auth
import katcha.db as db
from katcha.api.main import app
from katcha.config import Settings
from katcha.intelligence_models import ChannelProfile
from katcha.models import DomainEvent
from katcha.services.trends import (
    _signals_for_topic,
    evidence_readiness,
    refresh_channel_trends,
)
from katcha.trend_models import (
    ChannelTrendWatchVersion,
    TrendOpportunity,
    TrendSignal,
    TrendTopic,
    TrendTopicSignal,
)


def test_signal_api_filters_and_source_cap(monkeypatch):
    engine = create_engine(
        "sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    db.Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(db, "SessionLocal", factory)
    monkeypatch.setattr(auth, "get_settings", lambda: Settings(control_api_token=None))
    now = datetime.now(UTC)
    topic_id = uuid.uuid4()
    with factory.begin() as session:
        session.add(
            TrendTopic(
                id=topic_id,
                topic_key="fixture",
                display_name="Fixture",
                aliases=[],
                tags=[],
                first_seen_at=now,
                last_seen_at=now,
            )
        )
        for index in range(505):
            signal = TrendSignal(
                provider_key="reddit" if index < 500 else "youtube",
                external_id=str(index),
                observation_key="one",
                source_kind="community",
                independence_key=str(index),
                observed_at=now - timedelta(minutes=index),
                metrics={},
                media_refs=[],
                signal_metadata={},
            )
            session.add(signal)
            session.flush()
            session.add(TrendTopicSignal(trend_topic_id=topic_id, trend_signal_id=signal.id))
    with TestClient(app) as client:
        response = client.get(
            "/v1/trends/signals",
            params={
                "topic_id": str(topic_id),
                "provider_key": "YOUTUBE",
                "limit": 2,
            },
        )
        assert response.status_code == 200
        assert len(response.json()) == 2
        assert {row["provider_key"] for row in response.json()} == {"youtube"}
        assert client.get("/v1/trends/signals?limit=501").status_code == 422
        assert (
            client.get(
                "/v1/trends/signals",
                params={
                    "observed_after": now.isoformat(),
                    "observed_before": (now - timedelta(days=1)).isoformat(),
                },
            ).status_code
            == 422
        )
    signals, samples = _signals_for_topic(
        topic_id,
        cutoff=now - timedelta(days=1),
        as_of=now,
        source_weights={},
        platforms=["youtube"],
        languages=[],
        regions=[],
    )
    assert len(signals) == len(samples) == 5
    engine.dispose()


def test_readiness_deduplicates_observations_without_granting_rights():
    def signal(external_id, metadata):
        return TrendSignal(
            provider_key="youtube",
            external_id=external_id,
            observation_key=str(uuid.uuid4()),
            source_kind="video",
            independence_key="a",
            observed_at=datetime.now(UTC),
            metrics={},
            media_refs=[],
            signal_metadata=metadata,
        )

    value, summary = evidence_readiness(
        [
            signal("one", {}),
            signal("one", {"rights_ref": "editorial-review-1"}),
            signal("two", {}),
        ]
    )
    assert value == 0.5
    assert summary["entities_with_review_reference"] == 1
    assert "separate review" in summary["meaning"]


def test_confidence_change_event_only_on_material_new_snapshot(monkeypatch):
    engine = create_engine(
        "sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    db.Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(db, "SessionLocal", factory)
    now = datetime.now(UTC)
    channel_id = uuid.uuid4()
    topic_id = uuid.uuid4()
    with factory.begin() as session:
        session.add(
            ChannelProfile(id=channel_id, youtube_connection_id=uuid.uuid4(), status="active")
        )
        session.add(
            ChannelTrendWatchVersion(
                channel_profile_id=channel_id,
                version=1,
                interests=["gaming"],
                excluded_terms=[],
                entities=[],
                platforms=[],
                languages=[],
                regions=[],
                source_weights={},
                freshness_horizon_hours=72,
                min_confidence=Decimal("0"),
                opportunity_threshold=Decimal("0"),
                watch_metadata={},
            )
        )
        session.add(
            TrendTopic(
                id=topic_id,
                topic_key="gaming",
                display_name="gaming",
                aliases=[],
                tags=[],
                first_seen_at=now - timedelta(hours=2),
                last_seen_at=now,
            )
        )
        session.add(
            TrendOpportunity(
                channel_profile_id=channel_id,
                trend_topic_id=topic_id,
                watch_version=1,
                run_key="old",
                lifecycle="emerging",
                opportunity_score=Decimal("0.5"),
                confidence=Decimal("0"),
                components={},
                reasons=[],
                evidence_summary={},
                prediction_horizon_hours=24,
                expires_at=now + timedelta(days=1),
                created_at=now - timedelta(hours=1),
            )
        )
        signal = TrendSignal(
            provider_key="reddit",
            external_id="one",
            observation_key="one",
            source_kind="community",
            independence_key="r/gaming",
            observed_at=now,
            metrics={"comments": 100},
            media_refs=[],
            signal_metadata={},
        )
        session.add(signal)
        session.flush()
        session.add(TrendTopicSignal(trend_topic_id=topic_id, trend_signal_id=signal.id))

    refresh_channel_trends(channel_id, run_key="new", now=now)
    refresh_channel_trends(channel_id, run_key="new", now=now)
    with factory() as session:
        changes = list(
            session.scalars(
                select(DomainEvent).where(
                    DomainEvent.event_type == "trend.confidence.changed",
                )
            )
        )
        assert len(changes) == 1
        assert changes[0].payload["previous_confidence"] == 0
        assert changes[0].payload["watch_version"] == 1
        newest = session.scalar(select(TrendOpportunity).where(TrendOpportunity.run_key == "new"))
        assert newest.components["rights_reference_coverage"] == 0
        assert newest.evidence_summary["readiness"]["observed_entities"] == 1
    engine.dispose()
