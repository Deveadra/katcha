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
    list_resumable_source_runs,
    list_source_finds,
    restart_source_run,
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


def test_channel_youtube_source_without_profile_connection_uses_provider_fallback(
    source_scope,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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
                channel_id="UC" + "b" * 22,
                channel_title="Marvel Entertainment",
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
        source_key="youtube-channel-watch-provider-fallback",
        name="Marvel Entertainment",
        adapter_key="youtube",
        adapter_version="v1",
        platform="youtube",
        channel_profile_id=profile_id,
        query_template={"channel_reference": "@marvel", "limit": 25},
    )

    original_get = Session.get

    def get_without_connection(session, entity, ident, **kwargs):
        if entity is ChannelProfile and ident == profile_id:
            return type(
                "ProfileWithoutConnection",
                (),
                {"youtube_connection_id": None},
            )()
        return original_get(session, entity, ident, **kwargs)

    monkeypatch.setattr(Session, "get", get_without_connection)

    run = create_discovery_run_from_source(source.id)

    assert run.query["channel_reference"] == "@marvel"
    assert "youtube_connection_id" not in run.query


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


def test_manual_discovery_failure_preserves_provider_reason(
    source_scope,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from katcha.acquisition_models import DiscoveryRun
    from katcha.orchestration import discovery_activities

    class BrokenAdapter:
        def discover(self, query, cursor):
            del query, cursor
            raise ValueError(
                "YouTube discovery is not configured. Connect a YouTube channel "
                "or add a YouTube Data API key, then try again."
            )

    source = upsert_ingestion_source(
        source_key="broken-youtube-watch",
        name="Broken YouTube Watch",
        adapter_key="manifest",
        adapter_version="v1",
        platform="youtube",
    )
    run = create_discovery_run_from_source(source.id)
    monkeypatch.setattr(
        discovery_activities,
        "get_adapter",
        lambda *_args: BrokenAdapter(),
    )
    monkeypatch.setattr(
        discovery_activities,
        "record_discovery_run_failure",
        lambda *_args: None,
    )

    with pytest.raises(Exception, match="YouTube discovery is not configured"):
        discovery_activities.execute_discovery_page_activity(str(run.id))

    discovery_activities.mark_discovery_run_failed(
        str(run.id),
        "Activity task failed",
    )

    with source_scope() as session:
        failed = session.get(DiscoveryRun, run.id)
        assert failed is not None
        assert failed.status == "failed"
        assert failed.error is not None
        assert "Connect a YouTube channel" in failed.error
        assert "Activity task failed" not in failed.error


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



def test_source_library_search_and_pagination(source_scope) -> None:
    from katcha.services.ingestion_sources import list_ingestion_source_library

    first = upsert_ingestion_source(
        source_key="marvel-watch",
        name="Marvel Entertainment",
        adapter_key="youtube",
        adapter_version="v1",
        platform="youtube",
        usage_mode=SourceUsageMode.DISCOVERY_ONLY,
    )
    upsert_ingestion_source(
        source_key="ign-watch",
        name="IGN",
        adapter_key="youtube",
        adapter_version="v1",
        platform="youtube",
    )
    upsert_ingestion_source(
        source_key="reddit-gaming",
        name="Gaming Reddit",
        adapter_key="reddit",
        adapter_version="v1",
        platform="reddit",
        enabled=False,
    )

    page = list_ingestion_source_library(
        query="marvel",
        platform="youtube",
        limit=10,
        offset=0,
    )
    assert page.total == 1
    assert [row.id for row in page.items] == [first.id]

    youtube = list_ingestion_source_library(
        platform="youtube",
        sort="name",
        limit=1,
        offset=1,
    )
    assert youtube.total == 2
    assert len(youtube.items) == 1
    assert youtube.items[0].name == "Marvel Entertainment"

    paused = list_ingestion_source_library(enabled=False)
    assert paused.total == 1
    assert paused.items[0].source_key == "reddit-gaming"


def test_source_overview_includes_health_and_recent_finds(source_scope) -> None:
    from datetime import UTC, datetime

    from katcha.acquisition_models import (
        DiscoveryCandidate,
        DiscoveryObservation,
        DiscoveryRun,
    )
    from katcha.services.ingestion_sources import get_ingestion_source_overview

    source = upsert_ingestion_source(
        source_key="overview-source",
        name="Overview Source",
        adapter_key="manifest",
        adapter_version="v1",
        platform="web",
    )
    completed = create_discovery_run_from_source(source.id, idempotency_key="overview-complete")
    failed = create_discovery_run_from_source(source.id, idempotency_key="overview-failed")

    with source_scope() as session:
        completed_row = session.get(DiscoveryRun, completed.id)
        failed_row = session.get(DiscoveryRun, failed.id)
        assert completed_row is not None and failed_row is not None
        completed_row.status = "completed"
        completed_row.completed_at = datetime.now(UTC)
        failed_row.status = "failed"
        failed_row.error = "Provider unavailable"
        failed_row.completed_at = datetime.now(UTC)

        candidate = DiscoveryCandidate(
            adapter_key="manifest",
            external_id="overview-item",
            source_url="https://example.com/item",
            canonical_url="https://example.com/item",
            platform="web",
            status="discovered",
            title="Recent find",
            creator="Example Creator",
            provenance_confidence=0.9,
            provenance_claims={},
            candidate_metadata={},
        )
        session.add(candidate)
        session.flush()
        session.add(
            DiscoveryObservation(
                discovery_run_id=completed.id,
                discovery_candidate_id=candidate.id,
                external_id="overview-item",
                observation_metadata={},
            )
        )

    overview = get_ingestion_source_overview(source.id)
    assert overview.run_count == 2
    assert overview.status_counts["completed"] == 1
    assert overview.status_counts["failed"] == 1
    assert overview.discovery_count == 1
    assert overview.unique_candidate_count == 1
    assert overview.recent_runs[0].status in {"completed", "failed"}
    assert overview.recent_finds[0].candidate.title == "Recent find"



def test_specific_discovery_failure_message_prefers_provider_cause() -> None:
    from katcha.orchestration.discovery_workflows import _specific_failure_message

    provider = ValueError(
        "YouTube discovery is not configured. Connect a YouTube channel "
        "or add a YouTube Data API key."
    )
    activity = RuntimeError("Activity task failed")
    activity.__cause__ = provider
    workflow = RuntimeError("Workflow execution failed")
    workflow.__cause__ = activity

    assert _specific_failure_message(workflow).startswith(
        "YouTube discovery is not configured."
    )



def test_source_find_library_returns_all_unique_finds_with_search_and_pagination(
    source_scope,
) -> None:
    import uuid
    from datetime import UTC, datetime, timedelta

    from katcha.acquisition_models import DiscoveryObservation, DiscoveryRun

    source = upsert_ingestion_source(
        source_key="all-finds",
        name="All Finds",
        adapter_key="manifest",
        adapter_version="v1",
        platform="web",
    )
    first_run = create_discovery_run_from_source(
        source.id,
        idempotency_key="all-finds-first",
    )
    second_run = create_discovery_run_from_source(
        source.id,
        idempotency_key="all-finds-second",
    )
    base = datetime.now(UTC)
    with source_scope() as session:
        for index in range(30):
            candidate = DiscoveryCandidate(
                id=uuid.uuid4(),
                discovery_run_id=first_run.id,
                adapter_key="manifest",
                external_id=f"find-{index}",
                source_url=f"https://example.com/find/{index}",
                canonical_url=f"https://example.com/find/{index}",
                platform="web",
                status="discovered",
                title=(
                    "VisionQuest Official Trailer"
                    if index == 29
                    else f"Find {index:02d}"
                ),
                creator="Marvel Entertainment",
                provenance_confidence=0.9,
                provenance_claims={},
                candidate_metadata={},
            )
            session.add(candidate)
            session.flush()
            session.add(
                DiscoveryObservation(
                    discovery_run_id=first_run.id,
                    discovery_candidate_id=candidate.id,
                    observed_at=base - timedelta(minutes=index + 1),
                )
            )
            if index == 0:
                session.add(
                    DiscoveryObservation(
                        discovery_run_id=second_run.id,
                        discovery_candidate_id=candidate.id,
                        observed_at=base,
                    )
                )

    first_page = list_source_finds(source.id, limit=25, offset=0)
    assert first_page.total == 30
    assert len(first_page.items) == 25
    assert first_page.items[0].candidate.title == "Find 00"

    second_page = list_source_finds(source.id, limit=25, offset=25)
    assert second_page.total == 30
    assert len(second_page.items) == 5

    searched = list_source_finds(source.id, query="visionquest", limit=25)
    assert searched.total == 1
    assert searched.items[0].candidate.title == "VisionQuest Official Trailer"


def test_failed_source_run_restart_preserves_history_and_is_idempotent(
    source_scope,
) -> None:
    from katcha.acquisition_models import DiscoveryRun

    source = upsert_ingestion_source(
        source_key="restart-source",
        name="Restart Source",
        adapter_key="manifest",
        adapter_version="v1",
        platform="web",
        query_template={"items": []},
    )
    failed = create_discovery_run_from_source(
        source.id,
        idempotency_key="failed-attempt",
    )
    with source_scope() as session:
        row = session.get(DiscoveryRun, failed.id)
        assert row is not None
        row.status = "failed"
        row.error = "provider unavailable"

    restarted = restart_source_run(
        failed.id,
        idempotency_key="restart-attempt",
    )
    replay = restart_source_run(
        failed.id,
        idempotency_key="restart-attempt",
    )

    assert restarted.id != failed.id
    assert replay.id == restarted.id
    assert restarted.status == "queued"
    assert restarted.query == failed.query
    assert restarted.run_metadata["restarted_from_run_id"] == str(failed.id)
    assert restarted.run_metadata["recovery_mode"] == "manual_restart"

    with source_scope() as session:
        original = session.get(DiscoveryRun, failed.id)
        assert original is not None
        assert original.status == "failed"
        assert original.error == "provider unavailable"


def test_only_enabled_source_runs_are_resumable(source_scope) -> None:
    from katcha.acquisition_models import DiscoveryRun

    active = upsert_ingestion_source(
        source_key="resume-active",
        name="Resume Active",
        adapter_key="manifest",
        adapter_version="v1",
        platform="web",
    )
    paused = upsert_ingestion_source(
        source_key="resume-paused",
        name="Resume Paused",
        adapter_key="manifest",
        adapter_version="v1",
        platform="web",
        enabled=False,
    )
    queued = create_discovery_run_from_source(active.id, idempotency_key="queued-run")
    running = create_discovery_run_from_source(active.id, idempotency_key="running-run")

    # Temporarily enable the paused source to create a historical run, then pause it.
    paused_enabled = upsert_ingestion_source(
        source_key=paused.source_key,
        name=paused.name,
        adapter_key=paused.adapter_key,
        adapter_version=paused.adapter_version,
        platform=paused.platform,
        enabled=True,
    )
    paused_run = create_discovery_run_from_source(
        paused_enabled.id,
        idempotency_key="paused-run",
    )
    upsert_ingestion_source(
        source_key=paused.source_key,
        name=paused.name,
        adapter_key=paused.adapter_key,
        adapter_version=paused.adapter_version,
        platform=paused.platform,
        enabled=False,
    )

    with source_scope() as session:
        session.get(DiscoveryRun, running.id).status = "running"

    rows = list_resumable_source_runs()
    ids = {row.id for row in rows}
    assert queued.id in ids
    assert running.id in ids
    assert paused_run.id not in ids


@pytest.mark.asyncio
async def test_discovery_worker_reconciles_orphaned_runs_without_duplicate_replay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import uuid
    from types import SimpleNamespace

    from katcha.orchestration import discovery_worker

    class AlreadyStarted(Exception):
        pass

    run_one = SimpleNamespace(id=uuid.uuid4())
    run_two = SimpleNamespace(id=uuid.uuid4())
    monkeypatch.setattr(
        discovery_worker,
        "list_resumable_source_runs",
        lambda: [run_one, run_two],
    )
    monkeypatch.setattr(
        discovery_worker,
        "WorkflowAlreadyStartedError",
        AlreadyStarted,
    )

    class FakeClient:
        def __init__(self):
            self.calls = []

        async def start_workflow(self, workflow, run_id, **kwargs):
            self.calls.append((workflow, run_id, kwargs))
            if run_id == str(run_two.id):
                raise AlreadyStarted()
            return SimpleNamespace(id=kwargs["id"])

    client = FakeClient()
    resumed, already_present = await discovery_worker._resume_incomplete_source_runs(
        client
    )

    assert resumed == 1
    assert already_present == 1
    assert [call[1] for call in client.calls] == [
        str(run_one.id),
        str(run_two.id),
    ]
    assert all(
        call[2]["id"].startswith("discovery-run-")
        for call in client.calls
    )
