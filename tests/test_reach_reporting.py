from __future__ import annotations

import uuid
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from katcha import (  # noqa: F401
    longform_models,
    production_models,
    short_episode_models,
)
from katcha.api.main import app
from katcha.db import Base
from katcha.packaging_models import (
    PublicationPackagingActivation,
    PublicationPackagingVariant,
)
from katcha.publishing_models import Publication, YouTubeConnection
from katcha.reach_models import (
    PublicationReachObservation,
    YouTubeReachReportingJob,
)
from katcha.services import reach_reporting


@pytest.fixture
def reach_scope(monkeypatch: pytest.MonkeyPatch):
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

    monkeypatch.setattr(reach_reporting, "session_scope", scope)
    return scope


def _connection_and_publication(scope, suffix: str) -> tuple[YouTubeConnection, Publication]:
    with scope() as session:
        connection = YouTubeConnection(
            channel_id="channel-reach",
            channel_title="Reach Channel",
            status="active",
            scopes=["https://www.googleapis.com/auth/yt-analytics.readonly"],
            encrypted_access_token="encrypted",
            encrypted_refresh_token="encrypted",
            token_expires_at=datetime.now(UTC) + timedelta(hours=1),
        )
        session.add(connection)
        session.flush()
        publication = Publication(
            production_id=uuid.uuid4(),
            youtube_connection_id=connection.id,
            workflow_id=f"publication-{suffix}",
            analytics_workflow_id=f"analytics-{suffix}",
            title="Original title",
            description="",
            youtube_video_id=f"video-{suffix}",
        )
        session.add(publication)
        session.flush()
        session.expunge(connection)
        session.expunge(publication)
        return connection, publication


def _variant(scope, publication: Publication, key: str) -> PublicationPackagingVariant:
    with scope() as session:
        row = PublicationPackagingVariant(
            publication_id=publication.id,
            variant_key=key,
            version=1,
            title=f"Title {key}",
            description="",
            created_by="test",
        )
        session.add(row)
        session.flush()
        session.expunge(row)
        return row


def _activation(
    scope,
    publication: Publication,
    variant: PublicationPackagingVariant,
    applied_at: datetime,
) -> None:
    with scope() as session:
        session.add(
            PublicationPackagingActivation(
                publication_id=publication.id,
                variant_id=variant.id,
                activation_key=f"activation-{variant.variant_key}",
                workflow_id=f"workflow-{variant.id}",
                status="applied",
                stage="completed",
                youtube_video_id=str(publication.youtube_video_id),
                applied_at=applied_at,
            )
        )


def test_parse_reach_csv_requires_official_reach_columns() -> None:
    with pytest.raises(ValueError, match="missing required columns"):
        reach_reporting.parse_reach_csv(b"date,video_id\n2026-09-23,abc\n")


def test_reach_import_attributes_variant_mixed_and_legacy_by_pacific_day(
    reach_scope,
) -> None:
    connection, variant_publication = _connection_and_publication(reach_scope, "variant")
    with reach_scope() as session:
        mixed_publication = Publication(
            production_id=uuid.uuid4(),
            youtube_connection_id=connection.id,
            workflow_id="publication-mixed",
            analytics_workflow_id="analytics-mixed",
            title="Mixed",
            youtube_video_id="video-mixed",
        )
        legacy_publication = Publication(
            production_id=uuid.uuid4(),
            youtube_connection_id=connection.id,
            workflow_id="publication-legacy",
            analytics_workflow_id="analytics-legacy",
            title="Legacy",
            youtube_video_id="video-legacy",
        )
        session.add_all([mixed_publication, legacy_publication])
        session.flush()
        session.expunge(mixed_publication)
        session.expunge(legacy_publication)

    variant = _variant(reach_scope, variant_publication, "variant")
    mixed_variant = _variant(reach_scope, mixed_publication, "mixed")
    # 2026-09-23 starts at 07:00 UTC in Pacific daylight time.
    _activation(
        reach_scope,
        variant_publication,
        variant,
        datetime(2026, 9, 23, 6, 0, tzinfo=UTC),
    )
    _activation(
        reach_scope,
        mixed_publication,
        mixed_variant,
        datetime(2026, 9, 23, 12, 0, tzinfo=UTC),
    )

    with reach_scope() as session:
        job = YouTubeReachReportingJob(
            youtube_connection_id=connection.id,
            report_type_id="channel_reach_basic_a1",
            provider_job_id="provider-job",
            status="active",
            stage="active",
        )
        session.add(job)
        session.flush()
        job_id = job.id

    payload = (
        "date,channel_id,video_id,video_thumbnail_impressions,"
        "video_thumbnail_impressions_ctr\n"
        "2026-09-23,channel-reach,video-variant,1000,0.075\n"
        "2026-09-23,channel-reach,video-mixed,500,0.081\n"
        "2026-09-23,channel-reach,video-legacy,250,0.052\n"
    ).encode()
    result = reach_reporting.import_reach_report(
        connection.id,
        job_id,
        report={"id": "report-1", "jobId": "provider-job"},
        payload=payload,
    )
    assert result["observations_created"] == 3

    with reach_scope() as session:
        rows = list(
            session.scalars(
                select(PublicationReachObservation).order_by(
                    PublicationReachObservation.impressions.desc()
                )
            )
        )
        by_publication = {row.publication_id: row for row in rows}
        attributed = by_publication[variant_publication.id]
        mixed = by_publication[mixed_publication.id]
        legacy = by_publication[legacy_publication.id]
        assert attributed.attribution_status == "variant"
        assert attributed.packaging_variant_id == variant.id
        assert mixed.attribution_status == "mixed"
        assert mixed.packaging_variant_id is None
        assert legacy.attribution_status == "legacy"
        assert legacy.packaging_variant_id is None


def test_reimport_and_overlapping_reports_do_not_double_count(reach_scope) -> None:
    connection, publication = _connection_and_publication(reach_scope, "stable")
    with reach_scope() as session:
        job = YouTubeReachReportingJob(
            youtube_connection_id=connection.id,
            report_type_id="channel_reach_basic_a1",
            provider_job_id="provider-job",
            status="active",
            stage="active",
        )
        session.add(job)
        session.flush()
        job_id = job.id

    payload = (
        "date,channel_id,video_id,video_thumbnail_impressions,"
        "video_thumbnail_impressions_ctr\n"
        "2026-09-23,channel-reach,video-stable,100,0.05\n"
    ).encode()
    first = reach_reporting.import_reach_report(
        connection.id,
        job_id,
        report={"id": "report-a"},
        payload=payload,
    )
    replay = reach_reporting.import_reach_report(
        connection.id,
        job_id,
        report={"id": "report-a"},
        payload=payload,
    )
    overlap = reach_reporting.import_reach_report(
        connection.id,
        job_id,
        report={"id": "report-b"},
        payload=payload,
    )
    assert first["observations_created"] == 1
    assert replay["imported"] is False
    assert overlap["observations_created"] == 0

    changed = payload.replace(b",100,0.05", b",101,0.05")
    with pytest.raises(ValueError, match="overlapping reach reports disagree"):
        reach_reporting.import_reach_report(
            connection.id,
            job_id,
            report={"id": "report-c"},
            payload=changed,
        )

    with reach_scope() as session:
        count = len(list(session.scalars(select(PublicationReachObservation))))
        assert count == 1


def test_reach_routes_are_mounted() -> None:
    paths = set(app.openapi()["paths"])
    assert "/v1/integrations/youtube/{connection_id}/reach/sync" in paths
    assert "/v1/integrations/youtube/{connection_id}/reach/job" in paths
    assert "/v1/publications/{publication_id}/reach" in paths
    assert "/v1/integrations/youtube/{connection_id}/reach/imports" in paths
    assert "/v1/publications/{publication_id}/reach/summary" in paths



def test_reach_summary_uses_first_full_variant_days_and_weighted_ctr(
    reach_scope,
) -> None:
    connection, publication = _connection_and_publication(reach_scope, "summary")
    variant = _variant(reach_scope, publication, "summary")
    with reach_scope() as session:
        job = YouTubeReachReportingJob(
            youtube_connection_id=connection.id,
            report_type_id="channel_reach_basic_a1",
            provider_job_id="provider-job",
            status="active",
            stage="active",
        )
        session.add(job)
        session.flush()
        job_id = job.id
    _activation(
        reach_scope,
        publication,
        variant,
        datetime(2026, 9, 22, 6, 0, tzinfo=UTC),
    )
    payload = (
        "date,channel_id,video_id,video_thumbnail_impressions,"
        "video_thumbnail_impressions_ctr\n"
        "2026-09-23,channel-reach,video-summary,100,0.04\n"
        "2026-09-24,channel-reach,video-summary,300,0.08\n"
        "2026-09-25,channel-reach,video-summary,900,0.20\n"
    ).encode()
    reach_reporting.import_reach_report(
        connection.id,
        job_id,
        report={"id": "report-summary"},
        payload=payload,
    )

    summary = reach_reporting.reach_summary_for_publication(
        publication.id,
        maturity_days=2,
    )
    assert summary["mixed_days"] == 0
    assert summary["legacy_days"] == 0
    assert len(summary["variants"]) == 1
    variant_summary = summary["variants"][0]
    assert variant_summary["observed_full_days"] == 2
    assert variant_summary["impressions"] == 400
    assert variant_summary["weighted_ctr"] == "0.07000000"
