from __future__ import annotations

import uuid
from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from katcha.api.main import app
from katcha.db import Base
from katcha.intelligence_models import ChannelProfile
from katcha.packaging_models import PublicationPackagingVariant
from katcha.publishing_models import Publication, YouTubeConnection
from katcha.reach_models import PublicationReachObservation
from katcha.services import packaging_intelligence


@pytest.fixture
def intelligence_scope(monkeypatch: pytest.MonkeyPatch):
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

    monkeypatch.setattr(packaging_intelligence, "session_scope", scope)
    return scope


def _reach(
    day: date,
    *,
    variant_id: uuid.UUID | None,
    status: str = "variant",
    impressions: int | None = 1_000,
    ctr: Decimal | None = Decimal("0.06"),
) -> PublicationReachObservation:
    return PublicationReachObservation(
        publication_id=uuid.uuid4(),
        report_import_id=uuid.uuid4(),
        report_date=day,
        impressions=impressions,
        ctr=ctr,
        attribution_status=status,
        packaging_variant_id=variant_id,
    )


def test_reach_windows_require_contiguous_full_variant_days_and_exclude_mixed() -> None:
    variant_id = uuid.uuid4()
    start = date(2026, 9, 1)
    rows = [
        _reach(start + timedelta(days=offset), variant_id=variant_id)
        for offset in range(3)
    ]
    rows.append(
        _reach(
            start + timedelta(days=3),
            variant_id=None,
            status="mixed",
        )
    )
    rows.extend(
        _reach(start + timedelta(days=offset), variant_id=variant_id)
        for offset in range(4, 7)
    )

    assert packaging_intelligence._eligible_reach_windows(rows, 3) == [
        rows[:3],
        rows[4:7],
    ]
    assert packaging_intelligence._eligible_reach_windows(rows, 7) == []


def test_ctr_uplift_cannot_win_when_watch_quality_degrades() -> None:
    baseline = {
        "publication_id": "publication",
        "variant_id": "baseline",
        "window_id": "window-a",
        "window_end": "2026-09-07",
        "impressions": 5_000,
        "ctr": 0.05,
        "average_view_percentage": 70.0,
        "retention_50": 0.62,
        "monetary_scope_available": False,
        "publication_contribution_margin_usd": None,
    }
    candidate = {
        **baseline,
        "variant_id": "candidate",
        "window_id": "window-b",
        "window_end": "2026-09-14",
        "ctr": 0.065,
        "average_view_percentage": 60.0,
        "retention_50": 0.48,
    }

    result = packaging_intelligence._comparison(baseline, candidate)

    assert result["recommendation_type"] == "observe"
    assert "watch_percentage_degraded" in result["blockers"]
    assert "midpoint_retention_degraded" in result["blockers"]


def test_negative_margin_blocks_preference_when_monetary_scope_exists() -> None:
    baseline = {
        "publication_id": "publication",
        "variant_id": "baseline",
        "window_id": "window-a",
        "window_end": "2026-09-07",
        "impressions": 5_000,
        "ctr": 0.05,
        "average_view_percentage": 70.0,
        "retention_50": 0.62,
        "monetary_scope_available": True,
        "publication_contribution_margin_usd": "-0.50",
    }
    candidate = {
        **baseline,
        "variant_id": "candidate",
        "window_id": "window-b",
        "window_end": "2026-09-14",
        "ctr": 0.06,
        "average_view_percentage": 70.0,
        "retention_50": 0.62,
    }

    result = packaging_intelligence._comparison(baseline, candidate)

    assert result["recommendation_type"] == "observe"
    assert "publication_margin_negative" in result["blockers"]


def test_missing_monetary_scope_is_not_coerced_to_zero_margin() -> None:
    baseline = {
        "publication_id": "publication",
        "variant_id": "baseline",
        "window_id": "window-a",
        "window_end": "2026-09-07",
        "impressions": 5_000,
        "ctr": 0.05,
        "average_view_percentage": 70.0,
        "retention_50": 0.62,
        "monetary_scope_available": False,
        "publication_contribution_margin_usd": None,
    }
    candidate = {
        **baseline,
        "variant_id": "candidate",
        "window_id": "window-b",
        "window_end": "2026-09-14",
        "ctr": 0.06,
    }

    result = packaging_intelligence._comparison(baseline, candidate)

    assert result["recommendation_type"] == "prefer"
    assert result["blockers"] == []
    assert "monetary_scope_unavailable" in result["notes"]


def test_chronological_validation_never_claims_a_trained_optimizer() -> None:
    recommendations = [
        {
            "candidate_window_end": f"2026-09-{day:02d}",
            "recommendation_type": "prefer" if day % 2 else "observe",
        }
        for day in range(1, 11)
    ]

    result = packaging_intelligence._chronological_validation(recommendations)

    assert result["comparison_count"] == 10
    assert result["development_count"] == 8
    assert result["holdout_count"] == 2
    assert result["chronological_cutoff"] == "2026-09-08"
    assert result["optimizer_trained"] is False
    assert result["optimizer_reliable"] is False


def test_exact_variant_window_is_measured_once_and_reused(
    intelligence_scope,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with intelligence_scope() as session:
        connection = YouTubeConnection(
            channel_id="channel-package",
            channel_title="Package Channel",
            status="active",
            scopes=["https://www.googleapis.com/auth/yt-analytics.readonly"],
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
        publication = Publication(
            production_id=uuid.uuid4(),
            youtube_connection_id=connection.id,
            workflow_id="publish-package",
            analytics_workflow_id="analytics-package",
            title="Package",
            youtube_video_id="video-package",
        )
        session.add(publication)
        session.flush()
        variant = PublicationPackagingVariant(
            publication_id=publication.id,
            variant_key="baseline",
            version=1,
            title="Package baseline",
            created_by="test",
        )
        session.add(variant)
        session.flush()
        for offset in range(7):
            session.add(
                PublicationReachObservation(
                    publication_id=publication.id,
                    report_import_id=uuid.uuid4(),
                    report_date=date(2026, 9, 1) + timedelta(days=offset),
                    impressions=1_000,
                    ctr=Decimal("0.06"),
                    attribution_status="variant",
                    packaging_variant_id=variant.id,
                )
            )
        profile_id = profile.id

    basic_calls: list[tuple[date, date]] = []
    retention_calls: list[tuple[date, date]] = []

    def fake_basic(_connection_id, _video_id, start_date, end_date):
        basic_calls.append((start_date, end_date))
        return (
            {"views": 4_000, "averageViewPercentage": "68.5"},
            {"columnHeaders": [{"name": "views"}]},
        )

    def fake_retention(_connection_id, _video_id, start_date, end_date):
        retention_calls.append((start_date, end_date))
        return (
            [
                {
                    "elapsedVideoTimeRatio": "0.50",
                    "audienceWatchRatio": "0.61",
                }
            ],
            {"columnHeaders": [{"name": "elapsedVideoTimeRatio"}]},
        )

    monkeypatch.setattr(packaging_intelligence, "basic_video_metrics", fake_basic)
    monkeypatch.setattr(packaging_intelligence, "retention_curve", fake_retention)

    first = packaging_intelligence.refresh_packaging_intelligence(
        profile_id,
        run_key="first",
        maturity_days=7,
    )
    second = packaging_intelligence.refresh_packaging_intelligence(
        profile_id,
        run_key="second",
        maturity_days=7,
    )

    assert first.variant_window_count == 1
    assert second.variant_window_count == 1
    assert basic_calls == [(date(2026, 9, 1), date(2026, 9, 7))]
    assert retention_calls == [(date(2026, 9, 1), date(2026, 9, 7))]
    metric = first.variant_metrics[0]
    assert metric["impressions"] == 7_000
    assert metric["ctr"] == pytest.approx(0.06)
    assert metric["average_view_percentage"] == pytest.approx(68.5)
    assert metric["retention_50"] == pytest.approx(0.61)


def test_packaging_intelligence_routes_are_mounted() -> None:
    paths = set(app.openapi()["paths"])

    assert "/v1/channels/{channel_profile_id}/packaging-intelligence" in paths
    assert (
        "/v1/channels/{channel_profile_id}/packaging-intelligence/history"
        in paths
    )
