from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.requests import Request

import katcha.api.control_auth as auth
import katcha.api.operations as operations
import katcha.db as db
from katcha.api.main import app
from katcha.config import Settings
from katcha.intelligence_models import ChannelProfile
from katcha.models import DomainEvent
from katcha.production_models import Production
from katcha.publishing_models import Publication, PublicationAnalyticsSnapshot
from katcha.render_models import RenderAttempt
from katcha.trend_models import (
    ChannelTrendWatchVersion,
    TrendOpportunity,
    TrendTopic,
)


@pytest.fixture
def data(monkeypatch):
    engine = create_engine(
        "sqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    db.Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(db, "SessionLocal", factory)
    monkeypatch.setattr(auth, "get_settings", lambda: Settings(control_api_token=None))

    now = datetime.now(UTC)
    channel = uuid.uuid4()
    other = uuid.uuid4()
    youtube = uuid.uuid4()
    other_youtube = uuid.uuid4()
    clip = uuid.uuid4()
    other_clip = uuid.uuid4()

    attention_id = uuid.uuid4()
    active_id = uuid.uuid4()
    other_attention_id = uuid.uuid4()
    published_id = uuid.uuid4()
    failed_publication_id = uuid.uuid4()
    topic_id = uuid.uuid4()
    other_topic_id = uuid.uuid4()

    with factory.begin() as session:
        session.add_all(
            [
                ChannelProfile(
                    id=channel,
                    youtube_connection_id=youtube,
                    status="active",
                    timezone="America/Chicago",
                    profile_metadata={"channel_title": "RankSnaxx"},
                ),
                ChannelProfile(
                    id=other,
                    youtube_connection_id=other_youtube,
                    status="active",
                    timezone="UTC",
                    profile_metadata={"channel_title": "Other Channel"},
                ),
            ]
        )
        session.add_all(
            [
                ChannelTrendWatchVersion(
                    channel_profile_id=channel,
                    version=1,
                    interests=["old gaming"],
                ),
                ChannelTrendWatchVersion(
                    channel_profile_id=channel,
                    version=2,
                    interests=["xbox"],
                ),
                ChannelTrendWatchVersion(
                    channel_profile_id=other,
                    version=1,
                    interests=["movies"],
                ),
            ]
        )
        session.add_all(
            [
                Production(
                    id=attention_id,
                    clip_id=clip,
                    channel_profile_id=channel,
                    workflow_id="prod-attention",
                    status="rendering",
                    stage="render",
                    persona_key="fixture",
                    persona_version="1",
                    prompt_version="1",
                ),
                Production(
                    id=active_id,
                    clip_id=clip,
                    channel_profile_id=channel,
                    workflow_id="prod-active",
                    status="scripting",
                    stage="script",
                    persona_key="fixture",
                    persona_version="1",
                    prompt_version="1",
                ),
                Production(
                    id=other_attention_id,
                    clip_id=other_clip,
                    channel_profile_id=other,
                    workflow_id="prod-other",
                    status="failed",
                    stage="render",
                    persona_key="fixture",
                    persona_version="1",
                    prompt_version="1",
                    error="Other channel failure",
                ),
            ]
        )
        session.add(
            RenderAttempt(
                attempt_key="production:fixture:dead-letter",
                production_id=attention_id,
                channel_profile_id=channel,
                source_generation=1,
                attempt_number=1,
                status="dead_letter",
                stage="retry_exhausted",
                output_key="renders/attention.mp4",
                manifest_version="v1",
                error="Renderer exhausted retries",
                failure_count=3,
            )
        )
        session.add_all(
            [
                Publication(
                    id=published_id,
                    production_id=active_id,
                    youtube_connection_id=youtube,
                    workflow_id="publish-complete",
                    analytics_workflow_id="analytics-complete",
                    status="published",
                    stage="published",
                    title="Published fixture",
                    youtube_video_id="video-1",
                    published_at=now - timedelta(days=1),
                ),
                Publication(
                    id=failed_publication_id,
                    production_id=attention_id,
                    youtube_connection_id=youtube,
                    workflow_id="publish-failed",
                    analytics_workflow_id="analytics-failed",
                    status="failed",
                    stage="upload",
                    title="Failed publication",
                    failure_reason="YouTube upload failed",
                ),
            ]
        )
        session.add(
            PublicationAnalyticsSnapshot(
                publication_id=published_id,
                sample_key="fixture-24h",
                sampled_at=now,
                period_start=date.today() - timedelta(days=1),
                period_end=date.today(),
                views=125000,
                average_view_percentage=Decimal("72.5"),
                estimated_revenue=Decimal("14.25"),
            )
        )

        topic = TrendTopic(
            id=topic_id,
            topic_key="xbox-fixture",
            display_name="Xbox showcase surprise",
            aliases=[],
            tags=["gaming"],
            first_seen_at=now,
            last_seen_at=now,
        )
        other_topic = TrendTopic(
            id=other_topic_id,
            topic_key="other-fixture",
            display_name="Other channel topic",
            aliases=[],
            tags=["other"],
            first_seen_at=now,
            last_seen_at=now,
        )
        session.add_all([topic, other_topic])
        session.add_all(
            [
                TrendOpportunity(
                    channel_profile_id=channel,
                    trend_topic_id=topic_id,
                    watch_version=2,
                    run_key="ops-fixture",
                    lifecycle="accelerating",
                    opportunity_score=Decimal("0.91"),
                    confidence=Decimal("0.83"),
                    rank=1,
                    prediction_horizon_hours=24,
                    expires_at=now + timedelta(hours=12),
                    components={"acceleration": 0.9},
                    reasons=["Fast growth", "Multiple independent sources"],
                    evidence_summary={},
                ),
                TrendOpportunity(
                    channel_profile_id=channel,
                    trend_topic_id=topic_id,
                    watch_version=1,
                    run_key="ops-stale-watch",
                    lifecycle="accelerating",
                    opportunity_score=Decimal("0.999"),
                    confidence=Decimal("0.99"),
                    rank=1,
                    prediction_horizon_hours=24,
                    expires_at=now + timedelta(hours=12),
                    components={"acceleration": 1.0},
                    reasons=["Stale watch version must stay hidden"],
                    evidence_summary={},
                    created_at=now + timedelta(minutes=1),
                ),
                TrendOpportunity(
                    channel_profile_id=other,
                    trend_topic_id=other_topic_id,
                    watch_version=1,
                    run_key="ops-other",
                    lifecycle="accelerating",
                    opportunity_score=Decimal("0.99"),
                    confidence=Decimal("0.95"),
                    rank=1,
                    prediction_horizon_hours=24,
                    expires_at=now + timedelta(hours=12),
                    components={"acceleration": 1.0},
                    reasons=["Should never leak"],
                    evidence_summary={},
                ),
            ]
        )
        session.add_all(
            [
                DomainEvent(
                    aggregate_type="production",
                    aggregate_id=str(attention_id),
                    event_type="production.render_dead_lettered",
                    payload={"channel_profile_id": str(channel)},
                    created_at=now,
                ),
                DomainEvent(
                    aggregate_type="production",
                    aggregate_id=str(other_attention_id),
                    event_type="production.failed",
                    payload={"channel_profile_id": str(other)},
                    created_at=now,
                ),
            ]
        )

    yield SimpleNamespace(
        factory=factory,
        channel=channel,
        other=other,
        attention=attention_id,
        active=active_id,
        published=published_id,
        failed_publication=failed_publication_id,
    )
    engine.dispose()


def test_operations_routes_are_registered() -> None:
    paths = app.openapi()["paths"]
    assert "/v1/operations/overview" in paths
    assert "get" in paths["/v1/operations/overview"]


def test_operations_overview_returns_actionable_channel_state(data) -> None:
    client = TestClient(app)
    response = client.get(
        f"/v1/operations/overview?channel_profile_id={data.channel}&limit=12"
    )
    assert response.status_code == 200
    payload = response.json()

    assert payload["summary"] == {
        "active_channels": 1,
        "active_work": 1,
        "needs_attention": 2,
        "fresh_opportunities": 1,
        "published_last_7d": 1,
    }
    assert payload["channels"][0]["title"] == "RankSnaxx"
    assert payload["channels"][0]["needs_attention"] == 2
    assert payload["channels"][0]["active_work"] == 1

    attention_ids = {row["id"] for row in payload["attention"]}
    assert str(data.attention) in attention_ids
    assert str(data.failed_publication) in attention_ids
    render_failure = next(
        row for row in payload["attention"] if row["id"] == str(data.attention)
    )
    assert render_failure["message"] == "Renderer exhausted retries"
    assert render_failure["href"].startswith("/editing?channel=")

    assert [row["id"] for row in payload["active"]] == [str(data.active)]
    assert payload["opportunities"][0]["topic"] == "Xbox showcase surprise"
    assert payload["opportunities"][0]["opportunity_score"] == "0.910000"
    assert payload["summary"]["fresh_opportunities"] == 1
    assert "Stale watch version" not in str(payload["opportunities"])

    publication = payload["publications"][0]
    assert publication["id"] == str(data.published)
    assert publication["views"] == 125000
    assert publication["average_view_percentage"] == "72.500000"
    assert publication["estimated_revenue"] == "14.25000000"

    assert payload["activity"][0]["event_type"] == "production.render_dead_lettered"
    assert all(
        row["channel_profile_id"] == str(data.channel)
        for row in payload["activity"]
    )


def test_operations_overview_scopes_cross_channel_results(data, monkeypatch) -> None:
    monkeypatch.setattr(
        operations,
        "control_allowed_channel_ids",
        lambda _request: {data.channel},
    )
    client = TestClient(app)
    payload = client.get("/v1/operations/overview?limit=12").json()

    assert payload["summary"]["active_channels"] == 1
    assert {row["title"] for row in payload["channels"]} == {"RankSnaxx"}
    assert {row["topic"] for row in payload["opportunities"]} == {
        "Xbox showcase surprise"
    }
    assert all(
        row["channel_profile_id"] == str(data.channel)
        for row in payload["attention"] + payload["active"]
    )


def test_operations_overview_empty_state_is_stable(monkeypatch) -> None:
    engine = create_engine(
        "sqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    db.Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(db, "SessionLocal", factory)
    monkeypatch.setattr(auth, "get_settings", lambda: Settings(control_api_token=None))
    client = TestClient(app)

    response = client.get("/v1/operations/overview")
    assert response.status_code == 200
    payload = response.json()
    assert payload["summary"]["active_channels"] == 0
    assert payload["attention"] == []
    assert payload["active"] == []
    assert payload["opportunities"] == []
    assert payload["publications"] == []
    assert payload["activity"] == []
    engine.dispose()


def test_named_principal_operations_route_requires_channel_read_scope() -> None:
    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/v1/operations/overview",
        "raw_path": b"/v1/operations/overview",
        "query_string": b"",
        "headers": [],
        "client": ("test", 1),
        "server": ("test", 80),
    }
    request = Request(scope)
    request.state.control_principal_name = "reader"
    request.state.control_scopes = {"channels:read"}
    auth._require_named_principal_route_access(request)

    denied = Request(scope)
    denied.state.control_principal_name = "reader"
    denied.state.control_scopes = {"trends:read"}
    with pytest.raises(HTTPException) as exc:
        auth._require_named_principal_route_access(denied)
    assert exc.value.status_code == 403


def test_operations_limit_is_bounded(data) -> None:
    client = TestClient(app)
    assert client.get("/v1/operations/overview?limit=2").status_code == 422
    assert client.get("/v1/operations/overview?limit=51").status_code == 422
