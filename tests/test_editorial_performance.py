from __future__ import annotations

import uuid
from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from katcha.db import Base
from katcha.editorial_models import EditorialProject, EditorialRun
from katcha.intelligence_models import ChannelProfile
from katcha.packaging_models import PublicationPackagingVariant
from katcha.publishing_models import (
    Publication,
    PublicationAnalyticsSnapshot,
    RetentionPoint,
    YouTubeConnection,
)
from katcha.reach_models import PublicationReachObservation
from katcha.services import editorial_performance, editorial_runs


@pytest.fixture
def performance_scope(monkeypatch: pytest.MonkeyPatch):
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

    monkeypatch.setattr(editorial_performance, "session_scope", scope)
    monkeypatch.setattr(editorial_runs, "session_scope", scope)
    return scope


def _setup(performance_scope):
    with performance_scope() as session:
        connection = YouTubeConnection(
            channel_id="editorial-performance-channel",
            channel_title="Editorial Performance",
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
        )
        session.add(profile)
        session.flush()
        project = EditorialProject(
            id=uuid.uuid4(),
            channel_profile_id=profile.id,
            input_digest="a" * 64,
            brief={"prompt": "Measure the published editorial result"},
            revision=1,
        )
        session.add(project)
        session.flush()
        run = EditorialRun(
            id=uuid.uuid4(),
            project_id=project.id,
            channel_profile_id=profile.id,
            input_digest="b" * 64,
            input_revision=1,
            options={"target": "render"},
            attempt=1,
            status="completed",
            stage="render_ready_for_review",
            artifacts={},
            actor="test",
        )
        session.add(run)
        session.flush()
        return profile.id, project.id, run.id, connection.id


def test_editorial_performance_distinguishes_staged_from_missing_analytics(
    performance_scope,
) -> None:
    channel_id, project_id, run_id, connection_id = _setup(performance_scope)

    empty = editorial_performance.editorial_publication_performance(
        channel_id,
        project_id,
        run_id,
    )
    assert empty["measurement_state"] == "not_staged"
    assert empty["publication"] is None

    with performance_scope() as session:
        publication = Publication(
            editorial_run_id=run_id,
            youtube_connection_id=connection_id,
            workflow_id="performance-publication",
            analytics_workflow_id="performance-analytics",
            status="queued",
            stage="metadata_hold",
            title="Measured editorial result",
            treatment_metadata={"source_cost_usd_at_registration": "1.50000000"},
        )
        session.add(publication)

    held = editorial_performance.editorial_publication_performance(
        channel_id,
        project_id,
        run_id,
    )
    assert held["measurement_state"] == "not_uploaded"
    assert held["analytics"] is None
    assert held["reach"] is None
    assert held["economics"] == {
        "source_cost_usd": "1.50000000",
        "estimated_revenue_usd": None,
        "contribution_margin_usd": None,
    }


def test_editorial_performance_returns_measured_variant_retention_and_margin(
    performance_scope,
) -> None:
    channel_id, project_id, run_id, connection_id = _setup(performance_scope)

    with performance_scope() as session:
        publication = Publication(
            editorial_run_id=run_id,
            youtube_connection_id=connection_id,
            workflow_id="measured-publication",
            analytics_workflow_id="measured-analytics",
            status="published",
            stage="published",
            title="Measured editorial result",
            youtube_video_id="video-editorial-performance",
            published_at=datetime(2026, 10, 1, tzinfo=UTC),
            treatment_metadata={"source_cost_usd_at_registration": "1.50000000"},
        )
        session.add(publication)
        session.flush()
        variant = PublicationPackagingVariant(
            publication_id=publication.id,
            variant_key="hook-a",
            version=2,
            title="Measured title",
            thumbnail_storage_key="packaging/thumbnail.png",
            thumbnail_content_type="image/png",
            thumbnail_size_bytes=1234,
            thumbnail_sha256="c" * 64,
            created_by="test",
        )
        session.add(variant)
        session.flush()
        snapshot = PublicationAnalyticsSnapshot(
            publication_id=publication.id,
            sample_key="day-7",
            sampled_at=datetime(2026, 10, 7, tzinfo=UTC),
            period_start=date(2026, 10, 1),
            period_end=date(2026, 10, 7),
            views=12_000,
            engaged_views=10_000,
            estimated_minutes_watched=Decimal("42000"),
            average_view_duration=Decimal("210"),
            average_view_percentage=Decimal("67.5"),
            likes=800,
            comments=120,
            shares=44,
            subscribers_gained=95,
            subscribers_lost=4,
            estimated_revenue=Decimal("2.25"),
            monetized_playbacks=8_500,
        )
        session.add(snapshot)
        session.flush()
        session.add_all(
            [
                RetentionPoint(
                    snapshot_id=snapshot.id,
                    elapsed_video_time_ratio=Decimal("0.25"),
                    audience_watch_ratio=Decimal("0.81"),
                ),
                RetentionPoint(
                    snapshot_id=snapshot.id,
                    elapsed_video_time_ratio=Decimal("0.50"),
                    audience_watch_ratio=Decimal("0.62"),
                    relative_retention_performance=Decimal("0.08"),
                ),
            ]
        )
        session.add(
            PublicationReachObservation(
                publication_id=publication.id,
                report_import_id=uuid.uuid4(),
                report_date=date(2026, 10, 7),
                impressions=50_000,
                ctr=Decimal("0.071"),
                attribution_status="variant",
                packaging_variant_id=variant.id,
            )
        )

    result = editorial_performance.editorial_publication_performance(
        channel_id,
        project_id,
        run_id,
    )

    assert result["measurement_state"] == "measured"
    assert result["analytics"]["views"] == 12_000
    assert result["analytics"]["average_view_percentage"] == "67.500000"
    assert result["reach"]["impressions"] == 50_000
    assert result["reach"]["ctr"] == "0.07100000"
    assert result["retention_50"]["audience_watch_ratio"] == "0.62000000"
    assert result["packaging_variant"] == {
        "id": result["reach"]["packaging_variant_id"],
        "variant_key": "hook-a",
        "version": 2,
        "title": "Measured title",
        "has_thumbnail": True,
    }
    assert result["economics"] == {
        "source_cost_usd": "1.50000000",
        "estimated_revenue_usd": "2.25000000",
        "contribution_margin_usd": "0.75000000",
    }
