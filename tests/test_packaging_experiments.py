from __future__ import annotations

import inspect
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from katcha.api.main import app
from katcha.db import Base
from katcha.domain import AutomationLevel
from katcha.intelligence_models import AutomationPolicyVersion, ChannelProfile
from katcha.orchestration import intelligence_workflows
from katcha.packaging_intelligence_models import PackagingIntelligenceSnapshot
from katcha.packaging_models import (
    PackagingExperiment,
    PublicationPackagingActivation,
    PublicationPackagingVariant,
)
from katcha.publishing_models import Publication, YouTubeConnection
from katcha.services import packaging as packaging_service
from katcha.services import packaging_experiments


@pytest.fixture
def experiment_scope(monkeypatch: pytest.MonkeyPatch):
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)

    @contextmanager
    def scope():
        session: Session = factory()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    monkeypatch.setattr(packaging_experiments, "session_scope", scope)
    monkeypatch.setattr(packaging_service, "session_scope", scope)

    def latest(profile_id, *, maturity_days=None):
        with scope() as session:
            stmt = (
                select(PackagingIntelligenceSnapshot)
                .where(PackagingIntelligenceSnapshot.channel_profile_id == profile_id)
                .order_by(PackagingIntelligenceSnapshot.version.desc())
                .limit(1)
            )
            if maturity_days is not None:
                stmt = stmt.where(
                    PackagingIntelligenceSnapshot.maturity_days == maturity_days
                )
            row = session.scalar(stmt)
            if row is not None:
                session.expunge(row)
            return row

    monkeypatch.setattr(packaging_experiments, "latest_packaging_intelligence", latest)
    return scope


def _setup(
    scope,
    *,
    automation_level: AutomationLevel,
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID]:
    with scope() as session:
        connection = YouTubeConnection(
            channel_id=f"channel-{uuid.uuid4().hex[:8]}",
            channel_title="Experiment Channel",
            status="active",
            scopes=["youtube"],
            encrypted_access_token="encrypted",
            encrypted_refresh_token="encrypted",
            token_expires_at=datetime.now(UTC) + timedelta(hours=1),
        )
        session.add(connection)
        session.flush()
        profile = ChannelProfile(
            youtube_connection_id=connection.id,
            timezone="UTC",
            active_automation_version=1,
        )
        session.add(profile)
        session.flush()
        session.add(
            AutomationPolicyVersion(
                channel_profile_id=profile.id,
                version=1,
                level=automation_level.value,
            )
        )
        publication = Publication(
            production_id=uuid.uuid4(),
            youtube_connection_id=connection.id,
            workflow_id=f"publish-{uuid.uuid4()}",
            analytics_workflow_id=f"analytics-{uuid.uuid4()}",
            title="Baseline title",
            description="Baseline description",
            youtube_video_id=f"video-{uuid.uuid4().hex[:8]}",
        )
        session.add(publication)
        session.flush()
        baseline = PublicationPackagingVariant(
            publication_id=publication.id,
            variant_key="baseline",
            version=1,
            title="Baseline title",
            description="Baseline description",
            created_by="test",
        )
        candidate = PublicationPackagingVariant(
            publication_id=publication.id,
            variant_key="candidate",
            version=1,
            title="Candidate title",
            description="Candidate description",
            created_by="katcha-ai",
        )
        session.add_all([baseline, candidate])
        session.flush()
        publication.treatment_metadata = {
            "active_packaging": {
                "variant_id": str(baseline.id),
                "variant_key": baseline.variant_key,
                "version": baseline.version,
            }
        }
        snapshot = PackagingIntelligenceSnapshot(
            channel_profile_id=profile.id,
            version=1,
            run_key=f"run-{uuid.uuid4()}",
            maturity_days=7,
            publication_count=1,
            variant_window_count=1,
            recommendation_count=1,
            recommendation_status="advisory_recommendations",
            recommendations=[
                {
                    "publication_id": str(publication.id),
                    "baseline_variant_id": str(baseline.id),
                    "candidate_variant_id": str(candidate.id),
                    "recommendation_type": "test",
                    "blockers": [],
                    "notes": ["candidate_has_no_mature_measurement"],
                    "baseline_window_id": str(uuid.uuid4()),
                    "candidate_window_id": None,
                    "candidate_window_end": None,
                    "observed_delta": {},
                }
            ],
        )
        session.add(snapshot)
        session.flush()
        return publication.id, baseline.id, candidate.id, profile.id


def test_lower_automation_level_cannot_start_public_packaging_experiment(
    experiment_scope,
) -> None:
    publication_id, _baseline_id, _candidate_id, _profile_id = _setup(
        experiment_scope,
        automation_level=AutomationLevel.AUTO_PUBLISH_PRIVATE,
    )

    eligibility = packaging_experiments.experiment_eligibility(publication_id)

    assert eligibility.eligible is False
    assert "automation_level_not_scheduled" in eligibility.reasons


def test_scheduled_automation_starts_exact_recommended_candidate(
    experiment_scope,
) -> None:
    publication_id, baseline_id, candidate_id, _profile_id = _setup(
        experiment_scope,
        automation_level=AutomationLevel.AUTO_PUBLISH_SCHEDULED,
    )

    action = packaging_experiments.start_packaging_experiment(publication_id)

    assert action.action == "started"
    assert action.experiment is not None
    assert action.activation is not None
    assert action.experiment.baseline_variant_id == baseline_id
    assert action.experiment.candidate_variant_id == candidate_id
    assert action.experiment.status == "activating"
    assert action.activation.variant_id == candidate_id
    assert action.experiment.observe_after > datetime.now(UTC) + timedelta(days=6)


def test_mature_retention_damage_queues_exact_baseline_rollback(
    experiment_scope,
) -> None:
    publication_id, baseline_id, candidate_id, profile_id = _setup(
        experiment_scope,
        automation_level=AutomationLevel.AUTO_PUBLISH_SCHEDULED,
    )
    started = packaging_experiments.start_packaging_experiment(publication_id)
    assert started.experiment is not None
    assert started.activation is not None

    with experiment_scope() as session:
        activation = session.get(
            PublicationPackagingActivation,
            started.activation.id,
        )
        experiment = session.get(PackagingExperiment, started.experiment.id)
        assert activation is not None
        assert experiment is not None
        activation.status = "applied"
        activation.stage = "completed"
        activation.applied_at = datetime.now(UTC) - timedelta(days=8)
        experiment.status = "observing"
        experiment.stage = "candidate_observation"
        experiment.observe_after = datetime.now(UTC) - timedelta(hours=1)
        snapshot = PackagingIntelligenceSnapshot(
            channel_profile_id=profile_id,
            version=2,
            run_key=f"harm-{uuid.uuid4()}",
            maturity_days=7,
            publication_count=1,
            variant_window_count=2,
            recommendation_count=0,
            recommendation_status="evidence_available",
            recommendations=[
                {
                    "publication_id": str(publication_id),
                    "baseline_variant_id": str(baseline_id),
                    "candidate_variant_id": str(candidate_id),
                    "recommendation_type": "observe",
                    "blockers": [
                        "watch_percentage_degraded",
                        "midpoint_retention_degraded",
                    ],
                    "notes": [],
                    "baseline_window_id": str(uuid.uuid4()),
                    "candidate_window_id": str(uuid.uuid4()),
                    "candidate_window_end": "2026-09-24",
                    "observed_delta": {
                        "ctr_relative_uplift": 0.12,
                        "average_view_percentage": -8.0,
                        "retention_50": -0.09,
                    },
                }
            ],
        )
        session.add(snapshot)

    action = packaging_experiments.reconcile_packaging_experiment(
        started.experiment.id
    )

    assert action.action == "rollback"
    assert action.activation is not None
    assert action.activation.variant_id == baseline_id
    assert action.experiment is not None
    assert action.experiment.status == "rollback_queued"
    assert set(
        action.experiment.evidence_snapshot["rollback_blockers"]
    ) == {
        "watch_percentage_degraded",
        "midpoint_retention_degraded",
    }


def test_intelligence_workflow_demotes_before_packaging_mutation() -> None:
    source = inspect.getsource(intelligence_workflows._run_refresh)

    demotion = source.index('"apply_channel_safety_demotion_activity"')
    experiments = source.index('"run_packaging_experiments_activity"')

    assert demotion < experiments


def test_packaging_experiment_routes_are_mounted() -> None:
    paths = set(app.openapi()["paths"])
    assert (
        "/v1/publications/{publication_id}/packaging/experiment-eligibility"
        in paths
    )
    assert "/v1/publications/{publication_id}/packaging/experiments" in paths
    assert (
        "/v1/publications/{publication_id}/packaging/experiments/"
        "{experiment_id}/reconcile"
        in paths
    )
