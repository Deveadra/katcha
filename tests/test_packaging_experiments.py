from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import katcha.api.main  # noqa: F401  # Register all model tables for SQLite schema.
import katcha.api.packaging as packaging_api
import katcha.db as db
import katcha.orchestration.intelligence_workflows as intelligence_workflows
from katcha.api.main import app
from katcha.domain import AutomationLevel
from katcha.intelligence_models import AutomationPolicyVersion, ChannelProfile
from katcha.orchestration.packaging_activities import _require_automatic_experiment_policy
from katcha.packaging_intelligence_models import (
    PackagingIntelligenceSnapshot,
    PackagingVariantPerformanceWindow,
)
from katcha.packaging_models import (
    PackagingExperiment,
    PublicationPackagingActivation,
    PublicationPackagingVariant,
)
from katcha.publishing_models import Publication, YouTubeConnection
from katcha.services.packaging import register_packaging_activation
from katcha.services.packaging_experiments import (
    experiment_eligibility,
    reconcile_packaging_experiment,
    start_packaging_experiment,
)


@pytest.fixture
def records(monkeypatch):
    engine = create_engine(
        "sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    db.Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(db, "SessionLocal", factory)
    now = datetime.now(UTC)
    channel_id, publication_id, previous_id, candidate_id, snapshot_id, window_id = (
        uuid.uuid4() for _ in range(6)
    )
    with factory.begin() as session:
        connection = YouTubeConnection(
            channel_id="fixture",
            channel_title="Fixture",
            status="active",
            scopes=[],
            encrypted_access_token="fixture",
            encrypted_refresh_token="fixture",
            token_expires_at=now + timedelta(hours=1),
        )
        session.add(connection)
        session.flush()
        session.add(
            ChannelProfile(
                id=channel_id,
                youtube_connection_id=connection.id,
                status="active",
                active_automation_version=1,
            )
        )
        session.add(
            AutomationPolicyVersion(
                channel_profile_id=channel_id,
                version=1,
                level=AutomationLevel.AUTO_PUBLISH_SCHEDULED.value,
            )
        )
        session.add(
            Publication(
                id=publication_id,
                production_id=uuid.uuid4(),
                youtube_connection_id=connection.id,
                workflow_id="pub-fixture",
                analytics_workflow_id="analytics-fixture",
                status="published",
                title="Old",
                description="Old",
                youtube_video_id="video-fixture",
            )
        )
        session.add_all(
            [
                PublicationPackagingVariant(
                    id=previous_id,
                    publication_id=publication_id,
                    variant_key="old",
                    version=1,
                    title="Old",
                    description="Old",
                    variant_metadata={},
                ),
                PublicationPackagingVariant(
                    id=candidate_id,
                    publication_id=publication_id,
                    variant_key="new",
                    version=1,
                    title="New",
                    description="New",
                    variant_metadata={},
                ),
            ]
        )
        session.add(
            PublicationPackagingActivation(
                publication_id=publication_id,
                variant_id=previous_id,
                activation_key="old-applied",
                workflow_id="old-workflow",
                status="applied",
                stage="completed",
                youtube_video_id="video-fixture",
                applied_at=now - timedelta(days=10),
                created_at=now - timedelta(days=10),
            )
        )
        session.add(
            PackagingVariantPerformanceWindow(
                id=window_id,
                publication_id=publication_id,
                packaging_variant_id=previous_id,
                evidence_key="baseline",
                maturity_days=7,
                window_start=date.today() - timedelta(days=18),
                window_end=date.today() - timedelta(days=12),
                impressions=2000,
                ctr=Decimal("0.06"),
                average_view_percentage=Decimal("70"),
                retention_50=Decimal("0.65"),
                publication_cost_usd=Decimal("1"),
                evidence_metadata={},
            )
        )
        session.add(
            PackagingIntelligenceSnapshot(
                id=snapshot_id,
                channel_profile_id=channel_id,
                version=1,
                run_key="baseline",
                maturity_days=7,
                publication_count=1,
                variant_window_count=1,
                recommendation_count=1,
                recommendation_status="advisory_recommendations",
                variant_metrics=[],
                validation_metrics={"provider_error_count": 0},
                policy_snapshot={},
                created_at=now,
                recommendations=[
                    {
                        "publication_id": str(publication_id),
                        "baseline_variant_id": str(previous_id),
                        "candidate_variant_id": str(candidate_id),
                        "recommendation_type": "test",
                        "blockers": [],
                        "baseline_window_id": str(window_id),
                        "candidate_window_id": None,
                    }
                ],
            )
        )
    yield SimpleNamespace(
        factory=factory,
        now=now,
        channel=channel_id,
        publication=publication_id,
        previous=previous_id,
        candidate=candidate_id,
        baseline=window_id,
    )
    engine.dispose()


def test_policy_evidence_and_duplicate_claim(records):
    with TestClient(app) as client:
        response = client.get(
            f"/v1/publications/{records.publication}/packaging/experiments/eligibility",
            params={"candidate_variant_id": str(records.candidate)},
        )
        assert response.status_code == 200
        assert response.json()["eligible"] is True
    row = start_packaging_experiment(records.publication, records.candidate, now=records.now)
    assert row.status == "pending"
    again = start_packaging_experiment(records.publication, records.candidate, now=records.now)
    assert again.id == row.id
    with records.factory() as session:
        assert (
            session.scalar(
                select(PackagingExperiment).where(
                    PackagingExperiment.publication_id == records.publication
                )
            ).id
            == row.id
        )
    assert (
        experiment_eligibility(records.publication, records.candidate, now=records.now)["reason"]
        == "experiment_already_active"
    )
    with pytest.raises(ValueError, match="different packaging experiment"):
        start_packaging_experiment(records.publication, records.previous, now=records.now)
    with pytest.raises(ValueError, match="active packaging experiment"):
        register_packaging_activation(
            records.publication,
            variant_id=records.previous,
            activation_key="manual-while-experiment-running",
        )


def test_experiment_api_starts_and_lists_without_running_provider(records, monkeypatch):
    started = []

    async def fake_workflow(activation_id, workflow_id):
        started.append((activation_id, workflow_id))
        return workflow_id

    monkeypatch.setattr(packaging_api, "start_packaging_activation_workflow", fake_workflow)
    with TestClient(app) as client:
        response = client.post(
            f"/v1/publications/{records.publication}/packaging/experiments",
            json={"candidate_variant_id": str(records.candidate)},
        )
        assert response.status_code == 202
        assert response.json()["status"] == "pending"
        assert len(started) == 1
        listing = client.get(f"/v1/publications/{records.publication}/packaging/experiments")
        assert listing.status_code == 200
        assert [item["id"] for item in listing.json()] == [response.json()["id"]]
        refresh = client.post(
            f"/v1/publications/{records.publication}/packaging/experiments/"
            f"{response.json()['id']}/refresh"
        )
        assert refresh.status_code == 200
        assert len(started) == 2


def test_policy_blocks_automatic_mutation_and_missing_thumbnail_lineage(records):
    with records.factory.begin() as session:
        candidate = session.get(PublicationPackagingVariant, records.candidate)
        candidate.thumbnail_storage_key = "packaging/fixture/thumbnail.png"
        candidate.thumbnail_content_type = "image/png"
        candidate.thumbnail_size_bytes = 32
        candidate.thumbnail_sha256 = "a" * 64
    assert (
        experiment_eligibility(records.publication, records.candidate, now=records.now)["reason"]
        == "thumbnail_lineage_incomplete"
    )
    with records.factory.begin() as session:
        policy = session.scalar(select(AutomationPolicyVersion))
        policy.level = AutomationLevel.AUTO_PUBLISH_PRIVATE.value
    assert experiment_eligibility(records.publication, records.candidate, now=records.now)[
        "reason"
    ] == ("automation_policy_disallows_mutation")


def test_demoted_channel_cannot_mutate_automatic_activation(records):
    row = start_packaging_experiment(records.publication, records.candidate, now=records.now)
    with records.factory.begin() as session:
        session.get(
            AutomationPolicyVersion, session.scalar(select(AutomationPolicyVersion.id))
        ).level = AutomationLevel.REVIEW_REQUIRED.value
    with records.factory() as session:
        activation = session.get(PublicationPackagingActivation, row.activation_id)
        with pytest.raises(RuntimeError, match="no longer permits"):
            _require_automatic_experiment_policy(activation)
    with records.factory.begin() as session:
        session.get(PublicationPackagingActivation, row.activation_id).status = "failed"
    assert reconcile_packaging_experiment(row.id).status == "needs_review"


def test_guardrail_deterioration_requests_same_service_rollback(records):
    row = start_packaging_experiment(records.publication, records.candidate, now=records.now)
    with records.factory.begin() as session:
        activation = session.get(PublicationPackagingActivation, row.activation_id)
        activation.status = "applied"
        activation.applied_at = records.now - timedelta(days=9)
        window = PackagingVariantPerformanceWindow(
            publication_id=records.publication,
            packaging_variant_id=records.candidate,
            evidence_key="candidate",
            maturity_days=7,
            window_start=date.today() - timedelta(days=8),
            window_end=date.today() - timedelta(days=2),
            impressions=2000,
            ctr=Decimal("0.08"),
            average_view_percentage=Decimal("58"),
            retention_50=Decimal("0.4"),
            publication_cost_usd=Decimal("1"),
            evidence_metadata={},
        )
        session.add(window)
        session.flush()
        session.add(
            PackagingIntelligenceSnapshot(
                channel_profile_id=records.channel,
                version=2,
                run_key="mature",
                maturity_days=7,
                publication_count=1,
                variant_window_count=2,
                recommendation_count=0,
                recommendation_status="evidence_available",
                variant_metrics=[],
                validation_metrics={},
                policy_snapshot={},
                created_at=records.now + timedelta(minutes=1),
                recommendations=[
                    {
                        "publication_id": str(records.publication),
                        "baseline_variant_id": str(records.previous),
                        "candidate_variant_id": str(records.candidate),
                        "candidate_window_id": str(window.id),
                        "recommendation_type": "observe",
                        "blockers": ["watch_percentage_degraded"],
                    }
                ],
            )
        )
    result = reconcile_packaging_experiment(row.id, now=records.now + timedelta(hours=1))
    assert result.status == "rollback_pending"
    with records.factory.begin() as session:
        rollback = session.get(PublicationPackagingActivation, result.rollback_activation_id)
        assert rollback.variant_id == records.previous
        rollback.status = "applied"
    assert reconcile_packaging_experiment(row.id).status == "rolled_back"


@pytest.mark.asyncio
async def test_experiment_cadence_failure_does_not_stop_intelligence(monkeypatch):
    calls = []

    async def activity(name, *args, **kwargs):
        calls.append(name)
        if name == "run_channel_packaging_experiments_activity":
            raise RuntimeError("publishing task queue unavailable")
        return {"activity": name}

    monkeypatch.setattr(intelligence_workflows.workflow, "execute_activity", activity)
    monkeypatch.setattr(intelligence_workflows.workflow, "now", lambda: datetime.now(UTC))
    result = await intelligence_workflows._run_refresh(str(uuid.uuid4()), "cycle")
    assert result["packaging_experiments"] == {"status": "unavailable"}
    assert calls.index("run_channel_packaging_experiments_activity") > calls.index(
        "refresh_packaging_intelligence_activity"
    )
    assert calls[-2:] == [
        "apply_channel_safety_demotion_activity",
        "run_channel_packaging_experiments_activity",
    ]
