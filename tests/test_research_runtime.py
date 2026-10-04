"""Synthetic provider acceptance for the real saved-source -> trend handoff."""

import ast
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from katcha import db
from katcha.acquisition.adapters import DiscoveredCandidate, DiscoveryBatch, get_adapter
from katcha.acquisition_models import DiscoveryCandidate, DiscoveryRun, TopicWatchVersion
from katcha.api.acquisition import source_run_results
from katcha.config import Settings
from katcha.domain import SourceUsageMode
from katcha.intelligence_models import ChannelProfile
from katcha.orchestration.discovery_activities import (
    execute_discovery_page_activity,
    finalize_topic_watch_execution_activity,
)
from katcha.publishing_models import YouTubeConnection
from katcha.services.ingestion_sources import upsert_ingestion_source
from katcha.services.research import prepare_research_jobs
from katcha.services.trend_execution import prepare_topic_watch_execution
from katcha.services.trends import create_watch_profile
from katcha.trend_models import TrendSignal


@pytest.fixture
def research_db(monkeypatch):
    engine = create_engine("sqlite://", poolclass=StaticPool)
    db.load_model_metadata()
    db.Base.metadata.create_all(engine)
    monkeypatch.setattr(db, "SessionLocal", sessionmaker(bind=engine, expire_on_commit=False))
    monkeypatch.setattr("katcha.services.research.get_settings", lambda: Settings())
    yield
    engine.dispose()


def channel():
    with db.session_scope() as session:
        connection = YouTubeConnection(
            channel_id=f"fixture-{uuid.uuid4()}",
            channel_title="Research fixture",
            status="active",
            scopes=[],
            encrypted_access_token="fixture",
            encrypted_refresh_token="fixture",
            token_expires_at=datetime.now(UTC) + timedelta(days=1),
        )
        session.add(connection)
        session.flush()
        profile = ChannelProfile(
            youtube_connection_id=connection.id, status="active", timezone="UTC"
        )
        session.add(profile)
        session.flush()
        return profile.id, connection.id


@pytest.mark.parametrize("title", ["Gaming update", "🎬🔥", "---"])
def test_saved_source_collects_persists_and_bridges_to_trends(research_db, monkeypatch, title):
    profile_id, _ = channel()
    source = upsert_ingestion_source(
        source_key="fixture-feed",
        name="Gaming feed",
        adapter_key="rss_atom",
        adapter_version="v1",
        platform="web",
        channel_profile_id=profile_id,
        query_template={"feed_url": "https://example.com/feed", "include_terms": ["gaming"]},
    )
    monkeypatch.setattr(
        get_adapter("rss_atom", "v1"),
        "discover",
        lambda query, cursor: DiscoveryBatch(
            items=(
                DiscoveredCandidate(
                    source_url="https://example.com/gaming-news",
                    title=title,
                    creator="Fixture newsroom",
                    provenance_confidence=0.9,
                    metadata={
                        "published_at": datetime.now(UTC).isoformat(),
                        "source_metrics": {"mentions": 1},
                    },
                ),
            ),
        ),
    )
    jobs = prepare_research_jobs()
    assert len(jobs) == 1
    assert prepare_research_jobs() == jobs  # no new versions on each tick
    job = jobs[0]
    watch_id = uuid.UUID(job["topic_watch_id"])
    runs = prepare_topic_watch_execution(watch_id, execution_key=job["execution_key"])
    assert len(runs) == 1 and runs[0].action == "execute"
    result = execute_discovery_page_activity(str(runs[0].run_id))
    assert result["candidate_count"] == 1
    final = finalize_topic_watch_execution_activity(
        str(watch_id),
        job["execution_key"],
        [str(runs[0].run_id)],
        20,
    )
    assert final["status"] == "completed"
    assert final["signals_bridged"] == 1
    assert source_run_results(source.id, runs[0].run_id).total == 1
    with db.session_scope() as session:
        candidate = session.scalar(select(DiscoveryCandidate))
        assert candidate.candidate_metadata["channel_profile_id"] == str(profile_id)
        assert candidate.candidate_metadata["ingestion_source_id"] == str(source.id)
        assert session.scalar(select(TrendSignal)) is not None
        assert session.scalar(select(DiscoveryRun)).status == "completed"
    assert prepare_research_jobs() == []  # restart does not repeat this window


def test_interests_create_independent_channel_scoped_research(research_db):
    first, first_connection = channel()
    second, second_connection = channel()
    create_watch_profile(first, interests=["Xbox"])
    create_watch_profile(second, interests=["Movies"])
    jobs = prepare_research_jobs()
    assert len(jobs) == 2
    with db.session_scope() as session:
        watches = {
            row.channel_profile_id: row for row in session.scalars(select(TopicWatchVersion))
        }
        assert watches[first].include_terms == ["Xbox"]
        assert watches[second].include_terms == ["Movies"]
        assert watches[first].adapter_configs[0]["query"]["youtube_connection_id"] == str(
            first_connection
        )
        assert watches[second].adapter_configs[0]["query"]["youtube_connection_id"] == str(
            second_connection
        )
    create_watch_profile(first, interests=["Nintendo"])
    prepare_research_jobs()
    with db.session_scope() as session:
        assert len(list(session.scalars(select(TopicWatchVersion)))) == 3


def test_paused_blocked_and_manual_sources_do_not_auto_collect(research_db):
    for key, enabled, mode, adapter in [
        ("paused", False, SourceUsageMode.CANDIDATE_REVIEW, "rss_atom"),
        ("blocked", True, SourceUsageMode.BLOCKED, "rss_atom"),
        ("manual", True, SourceUsageMode.CANDIDATE_REVIEW, "operator_feed"),
    ]:
        upsert_ingestion_source(
            source_key=key,
            name=key,
            adapter_key=adapter,
            adapter_version="v1",
            platform="web",
            enabled=enabled,
            usage_mode=mode,
        )
    assert prepare_research_jobs() == []


def test_pause_between_dispatch_and_execution_prevents_provider_work(research_db):
    args = dict(
        source_key="pause-race",
        name="Feed",
        adapter_key="rss_atom",
        adapter_version="v1",
        platform="web",
    )
    upsert_ingestion_source(**args)
    job = prepare_research_jobs()[0]
    upsert_ingestion_source(**args, enabled=False)
    assert (
        prepare_topic_watch_execution(
            uuid.UUID(job["topic_watch_id"]),
            execution_key=job["execution_key"],
        )
        == []
    )


def test_consolidated_workers_register_every_called_activity():
    """Catch wiring drift even when isolated activity unit tests all pass."""
    root = Path("src/katcha/orchestration")
    for worker_name in [
        "worker.py",
        "analysis_worker.py",
        "production_worker.py",
        "intelligence_worker.py",
        "discovery_worker.py",
    ]:
        tree = ast.parse((root / worker_name).read_text())
        imports = {
            name.asname or name.name: node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
            for name in node.names
        }
        for node in ast.walk(tree):
            if (
                not isinstance(node, ast.Call)
                or not isinstance(node.func, ast.Name)
                or node.func.id != "Worker"
            ):
                continue
            kwargs = {kw.arg: kw.value for kw in node.keywords}
            activities = {item.id for item in kwargs["activities"].elts}
            for item in kwargs["workflows"].elts:
                module = imports[item.id].rsplit(".", 1)[-1]
                workflow_tree = ast.parse((root / f"{module}.py").read_text())
                for call in ast.walk(workflow_tree):
                    if (
                        isinstance(call, ast.Call)
                        and isinstance(call.func, ast.Attribute)
                        and call.func.attr == "execute_activity"
                        and call.args
                        and isinstance(call.args[0], ast.Constant)
                    ):
                        assert call.args[0].value in activities, (
                            worker_name,
                            item.id,
                            call.args[0].value,
                        )


def test_malformed_source_does_not_stop_other_research(research_db):
    from katcha.acquisition_models import IngestionSource

    broken = upsert_ingestion_source(
        source_key="malformed",
        name="Legacy",
        adapter_key="rss_atom",
        adapter_version="v1",
        platform="web",
    )
    with db.session_scope() as session:
        session.get(IngestionSource, broken.id).query_template = {"limit": "not a number"}
    good = upsert_ingestion_source(
        source_key="good",
        name="Working",
        adapter_key="rss_atom",
        adapter_version="v1",
        platform="web",
    )
    jobs = prepare_research_jobs()
    assert len(jobs) == 1
    with db.session_scope() as session:
        watch = session.get(TopicWatchVersion, uuid.UUID(jobs[0]["topic_watch_id"]))
        assert watch.adapter_configs[0]["ingestion_source_id"] == str(good.id)


def test_review_download_preserves_lineage_and_production_gate(research_db, monkeypatch, tmp_path):
    from types import SimpleNamespace

    from katcha.models import SourceItem
    from katcha.orchestration import activities
    from katcha.services.acquisition import promote_discovery_candidate

    channel_id, _ = channel()
    with db.session_scope() as session:
        candidate = DiscoveryCandidate(
            adapter_key="youtube",
            source_url="https://youtube.com/watch?v=fixture",
            canonical_url="https://youtube.com/watch?v=fixture",
            platform="youtube",
            candidate_metadata={"channel_profile_id": str(channel_id)},
        )
        session.add(candidate)
        session.flush()
        candidate_id = candidate.id
    source = promote_discovery_candidate(candidate_id, for_review=True)
    assert source.source_metadata["rights_basis"] == "unknown"
    assert promote_discovery_candidate(candidate_id, for_review=True).id == source.id
    with pytest.raises(ValueError, match="no rights assessment"):
        promote_discovery_candidate(candidate_id)
    directory = tmp_path / "isolated-download"
    directory.mkdir()
    path = directory / "video.mp4"
    path.write_bytes(b"synthetic media; no live download")
    monkeypatch.setattr(
        activities,
        "download",
        lambda url: SimpleNamespace(
            path=path,
            sha256="a" * 64,
            extension="mp4",
            size_bytes=path.stat().st_size,
            title="Fixture",
            creator="Fixture",
            platform="youtube",
            canonical_url=url,
            source_metadata={"channel_profile_id": "untrusted", "uploader": "fixture"},
            media_metadata={},
        ),
    )
    monkeypatch.setattr(
        activities,
        "ObjectStore",
        lambda: SimpleNamespace(
            ensure_bucket=lambda: None,
            raw_key=lambda *args: "raw/fixture.mp4",
            exists=lambda key: False,
            put_file=lambda *args: None,
        ),
    )
    result = activities.ingest_source(str(source.id))
    assert result["clip_id"]
    with db.session_scope() as session:
        saved = session.get(SourceItem, source.id)
        assert saved.source_metadata["channel_profile_id"] == str(channel_id)
        assert saved.source_metadata["discovery_candidate_id"] == str(candidate_id)
        assert saved.source_metadata["uploader"] == "fixture"
        assert saved.source_metadata["acquisition_purpose"] == "review"


def test_shared_source_cannot_inherit_provider_or_default_channel(research_db, monkeypatch):
    source = upsert_ingestion_source(
        source_key="unassigned",
        name="Shared",
        adapter_key="rss_atom",
        adapter_version="v1",
        platform="web",
        default_candidate_metadata={"channel_profile_id": str(uuid.uuid4())},
    )
    monkeypatch.setattr(
        get_adapter("rss_atom", "v1"),
        "discover",
        lambda query, cursor: DiscoveryBatch(
            items=(
                DiscoveredCandidate(
                    source_url="https://example.com/shared",
                    title="Shared story",
                    metadata={"channel_profile_id": str(uuid.uuid4())},
                ),
            )
        ),
    )
    job = prepare_research_jobs()[0]
    runs = prepare_topic_watch_execution(
        uuid.UUID(job["topic_watch_id"]),
        execution_key=job["execution_key"],
    )
    execute_discovery_page_activity(str(runs[0].run_id))
    with db.session_scope() as session:
        candidate = session.scalar(select(DiscoveryCandidate))
        assert "channel_profile_id" not in candidate.candidate_metadata
        assert candidate.candidate_metadata["source_scope"] == "shared"
        assert candidate.candidate_metadata["ingestion_source_id"] == str(source.id)


def test_unconfigured_channel_trends_wait_without_retries(research_db, monkeypatch):
    from katcha.orchestration import trend_activities
    from katcha.services.trends import refresh_channel_trends

    profile_id, _ = channel()
    result = refresh_channel_trends(profile_id, run_key="unconfigured")
    assert result["status"] == "awaiting_configuration"
    assert result["topics_scored"] == 0
    monkeypatch.setattr(trend_activities, "channel_trend_source_health", lambda _: {})
    monkeypatch.setattr(trend_activities, "source_health_allows_refresh", lambda *a, **k: True)
    result = trend_activities.refresh_channel_trends_activity(str(profile_id), "unconfigured")
    assert result["status"] == "awaiting_configuration"
    with pytest.raises(ValueError, match="channel profile not found"):
        refresh_channel_trends(uuid.uuid4(), run_key="missing")
