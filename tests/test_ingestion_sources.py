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
    create_source_import_run,
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
    assert run.run_metadata["source_scope"] == "shared"
    assert run.run_metadata["channel_profile_id"] is None
    assert run.run_metadata["default_candidate_metadata"]["source_scope"] == "shared"
    assert run.run_metadata["default_candidate_metadata"]["content_lane"] == "viral_clip"


def test_channel_youtube_source_freezes_profile_connection(source_scope) -> None:
    import uuid
    from datetime import UTC, datetime, timedelta

    from katcha.intelligence_models import ChannelProfile
    from katcha.publishing_models import YouTubeConnection

    connection_id = uuid.uuid4()
    profile_id = uuid.uuid4()
    with source_scope() as session:
        session.add(
            YouTubeConnection(
                id=connection_id,
                channel_id="UC" + "a" * 22,
                channel_title="RankSnaxx",
                status="active",
                scopes=["https://www.googleapis.com/auth/youtube"],
                encrypted_access_token="encrypted-access",
                encrypted_refresh_token="encrypted-refresh",
                token_expires_at=datetime.now(UTC) + timedelta(hours=1),
            )
        )
        session.add(
            ChannelProfile(
                id=profile_id,
                youtube_connection_id=connection_id,
                status="active",
                timezone="UTC",
            )
        )

    source = upsert_ingestion_source(
        source_key="youtube-channel-watch",
        name="Creator Watch",
        adapter_key="youtube",
        adapter_version="v1",
        platform="youtube",
        channel_profile_id=profile_id,
        query_template={"channel_reference": "@creator", "limit": 25},
    )

    run = create_discovery_run_from_source(source.id)

    assert run.query["youtube_connection_id"] == str(connection_id)


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
        assert candidate.candidate_metadata["source_scope"] == "shared"
        assert "channel_profile_id" not in candidate.candidate_metadata
        assert candidate.candidate_metadata["content_lane"] == "reel_candidate"
        assert candidate.candidate_metadata["source_metrics"] == {"views": 50000}


def test_operator_feed_source_flows_platform_hints_to_candidates(source_scope) -> None:
    source = upsert_ingestion_source(
        source_key="rank-snaxx-short-drops",
        name="RankSnaxx Short Drops",
        adapter_key="operator_feed",
        adapter_version="v1",
        platform="mixed",
        usage_mode=SourceUsageMode.OPERATOR_AUTHORIZED,
        query_template={
            "feed_key": "manual-ranksnaxx-drops",
            "default_platform": "tiktok",
            "default_content_kind": "rank_clip",
            "items": [
                {
                    "source_url": "https://www.tiktok.com/@creator/video/123",
                    "external_id": "tt-123",
                    "tags": ["fails", "sports"],
                    "metrics": {"views": 900000},
                }
            ],
        },
        default_candidate_metadata={"content_lane": "operator_drop"},
    )
    run = create_discovery_run_from_source(source.id, idempotency_key="drop-cycle-1")

    result = execute_discovery_page_activity(str(run.id))

    assert result["candidate_count"] == 1
    with source_scope() as session:
        candidate = session.scalar(select(DiscoveryCandidate))
        assert candidate is not None
        assert candidate.platform == "tiktok"
        assert candidate.candidate_metadata["source_platform"] == "mixed"
        assert candidate.candidate_metadata["source_usage_mode"] == "operator_authorized"
        assert candidate.candidate_metadata["platform_hint"] == "tiktok"
        assert candidate.candidate_metadata["content_kind"] == "rank_clip"
        assert candidate.candidate_metadata["content_lane"] == "operator_drop"
        assert candidate.candidate_metadata["tags"] == ["fails", "sports"]
        assert candidate.candidate_metadata["source_metrics"] == {"views": 900000}


def test_source_import_run_batches_operator_feed_urls(source_scope) -> None:
    source = upsert_ingestion_source(
        source_key="rank-snaxx-imports",
        name="RankSnaxx Imports",
        adapter_key="operator_feed",
        adapter_version="v1",
        platform="mixed",
        usage_mode=SourceUsageMode.OPERATOR_AUTHORIZED,
        query_template={
            "feed_key": "saved-feed",
            "default_platform": "tiktok",
            "default_content_kind": "rank_clip",
            "default_metadata": {"content_lane": "saved_lane"},
        },
    )

    result = create_source_import_run(
        source.id,
        batch_key="drop-2026-09-26-01",
        urls=["https://www.tiktok.com/@creator/video/123"],
        items=[
            {
                "source_url": "https://www.instagram.com/reel/example/",
                "platform": "instagram",
                "metrics": {"views": 120000},
            }
        ],
        default_metadata={"operator": "sundance"},
    )

    assert result.batch_key == "drop-2026-09-26-01"
    assert result.item_count == 2
    run = result.discovery_run
    assert run.run_key == "source-import:rank-snaxx-imports:drop-2026-09-26-01"
    assert run.query["feed_key"] == "saved-feed"
    assert run.query["default_metadata"] == {
        "content_lane": "saved_lane",
        "operator": "sundance",
        "source_import_batch_key": "drop-2026-09-26-01",
        "source_import_item_count": 2,
    }
    assert run.run_metadata["source_import_batch_key"] == "drop-2026-09-26-01"

    duplicate = create_source_import_run(
        source.id,
        batch_key="drop-2026-09-26-01",
        urls=["https://www.tiktok.com/@creator/video/456"],
    )
    assert duplicate.discovery_run.id == run.id


def test_source_import_run_metadata_flows_to_candidates(source_scope) -> None:
    source = upsert_ingestion_source(
        source_key="rank-snaxx-import-flow",
        name="RankSnaxx Import Flow",
        adapter_key="operator_feed",
        adapter_version="v1",
        platform="mixed",
        usage_mode=SourceUsageMode.OPERATOR_AUTHORIZED,
        query_template={
            "feed_key": "rank-snaxx-drops",
            "default_platform": "tiktok",
        },
    )
    result = create_source_import_run(
        source.id,
        batch_key="drop-flow",
        urls=["https://www.tiktok.com/@creator/video/123"],
    )

    activity_result = execute_discovery_page_activity(str(result.discovery_run.id))

    assert activity_result["candidate_count"] == 1
    with source_scope() as session:
        candidate = session.scalar(select(DiscoveryCandidate))
        assert candidate is not None
        assert candidate.candidate_metadata["source_import_batch_key"] == "drop-flow"
        assert candidate.candidate_metadata["source_import_item_count"] == 1
        assert candidate.candidate_metadata["operator_feed_key"] == "rank-snaxx-drops"


def test_source_import_run_rejects_empty_or_non_operator_sources(source_scope) -> None:
    source = upsert_ingestion_source(
        source_key="rank-snaxx-manifest",
        name="RankSnaxx Manifest",
        adapter_key="manifest",
        adapter_version="v1",
        platform="mixed",
        usage_mode=SourceUsageMode.OPERATOR_AUTHORIZED,
    )

    with pytest.raises(ValueError, match="at least one url or item"):
        create_source_import_run(source.id, urls=[])

    with pytest.raises(ValueError, match="require operator_feed"):
        create_source_import_run(source.id, urls=["https://example.com/video"])


def test_ingestion_source_rejects_uninstalled_adapter(source_scope) -> None:
    with pytest.raises(ValueError, match="not installed"):
        upsert_ingestion_source(
            source_key="future-site",
            name="Future Site",
            adapter_key="future",
            adapter_version="v1",
            platform="future",
        )


def test_import_replaces_both_template_collections(source_scope):
    source = upsert_ingestion_source(
        source_key="isolation", name="Isolation", adapter_key="operator_feed",
        adapter_version="v1", platform="custom",
        query_template={"items": [{"source_url": "https://example.com/old-item"}],
                        "urls": ["https://example.com/old-url"]},
    )
    urls_only = create_source_import_run(source.id, urls=["https://example.com/new"])
    assert urls_only.discovery_run.query["items"] == []
    assert urls_only.discovery_run.query["urls"] == ["https://example.com/new"]
    items_only = create_source_import_run(
        source.id, items=[{"source_url": "https://example.com/new-item"}],
    )
    assert items_only.discovery_run.query["urls"] == []
    assert items_only.discovery_run.query["items"] == [
        {"source_url": "https://example.com/new-item"}
    ]


def test_source_history_is_scoped_bounded_and_includes_failures(source_scope):
    from katcha.acquisition_models import DiscoveryRun
    from katcha.services.ingestion_sources import list_source_runs

    first = upsert_ingestion_source(
        source_key="first", name="First", adapter_key="manifest",
        adapter_version="v1", platform="custom",
    )
    second = upsert_ingestion_source(
        source_key="second", name="Second", adapter_key="manifest",
        adapter_version="v1", platform="custom",
    )
    older = create_discovery_run_from_source(first.id)
    run = create_discovery_run_from_source(first.id)
    create_discovery_run_from_source(second.id)
    with source_scope() as session:
        from datetime import timedelta

        row = session.get(DiscoveryRun, run.id)
        session.get(DiscoveryRun, older.id).created_at = row.created_at - timedelta(minutes=1)
        row.status = "failed"
        row.error = "Provider unavailable"
    rows = list_source_runs(first.id, limit=1)
    assert [row.id for row in rows] == [run.id]
    assert rows[0].error == "Provider unavailable"
    assert rows[0].status == "failed"
    import uuid
    with pytest.raises(ValueError, match="not found"):
        list_source_runs(uuid.uuid4())


def test_source_run_results_follow_observations_and_reject_other_sources(
    source_scope, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import uuid

    from fastapi import HTTPException

    from katcha.acquisition_models import DiscoveryObservation
    from katcha.api import acquisition

    first = upsert_ingestion_source(
        source_key="result-first", name="First", adapter_key="manifest",
        adapter_version="v1", platform="web",
    )
    second = upsert_ingestion_source(
        source_key="result-second", name="Second", adapter_key="manifest",
        adapter_version="v1", platform="web",
    )
    original = create_discovery_run_from_source(first.id)
    repeated = create_discovery_run_from_source(first.id)
    foreign = create_discovery_run_from_source(second.id)
    candidate_id = uuid.uuid4()
    with source_scope() as session:
        session.add(DiscoveryCandidate(
            id=candidate_id, discovery_run_id=original.id, adapter_key="manifest",
            source_url="https://example.com/post", canonical_url="https://example.com/post",
            platform="web", title="A discovered post",
        ))
        session.flush()
        session.add_all([
            DiscoveryObservation(discovery_run_id=original.id, discovery_candidate_id=candidate_id),
            DiscoveryObservation(discovery_run_id=repeated.id, discovery_candidate_id=candidate_id),
        ])
    monkeypatch.setattr(acquisition, "session_scope", source_scope)
    result = acquisition.source_run_results(first.id, repeated.id)
    assert result.total == 1
    assert result.candidates[0].source_url == "https://example.com/post"
    assert acquisition.source_run_results(second.id, foreign.id).total == 0
    with pytest.raises(HTTPException) as exc:
        acquisition.source_run_results(second.id, repeated.id)
    assert exc.value.status_code == 404


def test_create_only_does_not_overwrite_existing_source(source_scope):
    original = upsert_ingestion_source(
        source_key="unique", name="Original", adapter_key="manifest",
        adapter_version="v1", platform="custom",
    )
    with pytest.raises(ValueError, match="already exists"):
        upsert_ingestion_source(
            source_key="unique", name="Replacement", adapter_key="manifest",
            adapter_version="v1", platform="custom", create_only=True,
        )
    from katcha.acquisition_models import IngestionSource
    with source_scope() as session:
        assert session.get(IngestionSource, original.id).name == "Original"
