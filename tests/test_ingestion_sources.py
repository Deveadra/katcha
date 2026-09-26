from collections.abc import Iterator
from contextlib import contextmanager

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from katcha import db
from katcha.acquisition_models import DiscoveryCandidate
from katcha.domain import SourceUsageMode
from katcha.orchestration.discovery_activities import execute_discovery_page_activity
from katcha.services.ingestion_sources import (
    create_discovery_run_from_source,
    upsert_ingestion_source,
)


@pytest.fixture()
def source_scope(monkeypatch: pytest.MonkeyPatch):
    engine = create_engine(
        "sqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    db.load_model_metadata()
    db.Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(db, "SessionLocal", factory)

    @contextmanager
    def scope() -> Iterator[Session]:
        session = factory()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    return scope


def test_ingestion_source_creates_discovery_run_with_source_metadata(
    source_scope,
) -> None:
    source = upsert_ingestion_source(
        source_key="rank-snaxx-tiktok-watch",
        name="RankSnaxx TikTok Watch",
        adapter_key="manifest",
        adapter_version="v1",
        platform="tiktok",
        usage_mode=SourceUsageMode.OPERATOR_AUTHORIZED,
        query_template={
            "items": [
                {
                    "source_url": "https://www.tiktok.com/@creator/video/123",
                    "external_id": "tt-123",
                    "title": "Wild finish",
                }
            ]
        },
        default_candidate_metadata={"content_lane": "viral_clip"},
        source_metadata={"owner": "operator"},
        poll_interval_minutes=15,
    )

    run = create_discovery_run_from_source(source.id)

    assert run.adapter_key == "manifest"
    assert run.query["items"][0]["external_id"] == "tt-123"
    assert run.run_metadata["ingestion_source_key"] == "rank-snaxx-tiktok-watch"
    assert run.run_metadata["source_platform"] == "tiktok"
    assert run.run_metadata["source_usage_mode"] == "operator_authorized"
    assert run.run_metadata["default_candidate_metadata"]["content_lane"] == "viral_clip"


def test_ingestion_source_metadata_flows_to_candidates(source_scope) -> None:
    source = upsert_ingestion_source(
        source_key="rank-snaxx-ig-watch",
        name="RankSnaxx IG Watch",
        adapter_key="manifest",
        adapter_version="v1",
        platform="instagram",
        usage_mode=SourceUsageMode.CANDIDATE_REVIEW,
        query_template={
            "items": [
                {
                    "source_url": "https://www.instagram.com/reel/example/",
                    "external_id": "ig-1",
                    "metadata": {"source_metrics": {"views": 50000}},
                }
            ]
        },
        default_candidate_metadata={"content_lane": "reel_candidate"},
    )
    run = create_discovery_run_from_source(source.id, idempotency_key="ig-cycle-1")

    result = execute_discovery_page_activity(str(run.id))

    assert result["candidate_count"] == 1
    with source_scope() as session:
        candidate = session.scalar(select(DiscoveryCandidate))
        assert candidate is not None
        assert candidate.platform == "instagram"
        assert candidate.candidate_metadata["ingestion_source_key"] == "rank-snaxx-ig-watch"
        assert candidate.candidate_metadata["source_platform"] == "instagram"
        assert candidate.candidate_metadata["source_usage_mode"] == "candidate_review"
        assert candidate.candidate_metadata["content_lane"] == "reel_candidate"
        assert candidate.candidate_metadata["source_metrics"] == {"views": 50000}


def test_ingestion_source_rejects_uninstalled_adapter(source_scope) -> None:
    with pytest.raises(ValueError, match="not installed"):
        upsert_ingestion_source(
            source_key="future-site",
            name="Future Site",
            adapter_key="future",
            adapter_version="v1",
            platform="future",
        )
