from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from katcha import db
from katcha.acquisition_models import (
    IntelligenceBatchRecord,
    IntelligenceIngestBatch,
    IntelligenceRecord,
)
from katcha.intelligence_models import ChannelProfile
from katcha.publishing_models import YouTubeConnection
from katcha.services.command_resources import resolve_command_resources
from katcha.services.ingestion_sources import (
    get_intelligence_record,
    ingest_intelligence_batch,
    list_intelligence_records,
)


@pytest.fixture()
def intelligence_scope(monkeypatch: pytest.MonkeyPatch):
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


def _create_channel(scope, *, suffix: str, title: str) -> uuid.UUID:
    connection_id = uuid.uuid4()
    channel_profile_id = uuid.uuid4()
    with scope() as session:
        session.add(
            YouTubeConnection(
                id=connection_id,
                channel_id=f"UC{suffix:0<22}"[:24],
                channel_title=title,
                status="active",
                scopes=["https://www.googleapis.com/auth/youtube"],
                encrypted_access_token="encrypted-access",
                encrypted_refresh_token="encrypted-refresh",
                token_expires_at=datetime.now(UTC) + timedelta(hours=1),
            )
        )
        session.add(
            ChannelProfile(
                id=channel_profile_id,
                youtube_connection_id=connection_id,
                status="active",
                timezone="America/Chicago",
            )
        )
    return channel_profile_id


def _competitor_record(*, summary: str = "Fast gaming-news packaging") -> dict[str, object]:
    return {
        "record_kind": "channel",
        "record_key": "youtube:channel:UC-competitor",
        "title": "Competitor Gaming Channel",
        "summary": summary,
        "source_url": "https://www.youtube.com/@competitor",
        "platform": "youtube",
        "status": "active",
        "tags": ["competitor", "gaming_news"],
        "payload": {
            "content_lanes": ["gaming news", "shorts"],
            "observed_patterns": {"hook_seconds": 2.0, "caption_density": "high"},
        },
        "provenance": {
            "collector": "orion",
            "method": "manual_research",
            "confidence": 0.94,
        },
        "observed_at": "2026-09-30T16:00:00Z",
    }


def test_intelligence_batch_persists_channel_scoped_grounding(intelligence_scope) -> None:
    channel_id = _create_channel(
        intelligence_scope,
        suffix="ranksnaxx",
        title="RankSnaxx",
    )

    result = ingest_intelligence_batch(
        channel_profile_id=channel_id,
        batch_key="orion-2026-09-30-opportunities-001",
        producer="orion",
        source_type="assistant",
        batch_metadata={"purpose": "interim_rank_snaxx_operations"},
        records=[_competitor_record()],
    )

    assert result.replayed is False
    assert result.created_count == 1
    assert result.updated_count == 0
    assert result.batch.record_count == 1
    record = result.records[0]
    assert record.channel_profile_id == channel_id
    assert record.record_kind == "channel"
    assert record.tags == ["competitor", "gaming_news"]
    assert record.provenance["collector"] == "orion"

    listed = list_intelligence_records(
        channel_id,
        record_kind="channel",
        record_status="active",
    )
    assert [item.id for item in listed] == [record.id]

    evidence = resolve_command_resources(
        channel_id,
        [("intelligence_record", record.id)],
    )
    assert evidence[0]["kind"] == "intelligence_record"
    assert evidence[0]["record_key"] == "youtube:channel:UC-competitor"
    assert evidence[0]["payload"]["observed_patterns"]["hook_seconds"] == 2.0
    assert evidence[0]["context_source"] == "typed_resource"


def test_intelligence_batch_replay_is_idempotent_and_conflict_safe(
    intelligence_scope,
) -> None:
    channel_id = _create_channel(
        intelligence_scope,
        suffix="replay",
        title="Replay Test",
    )
    kwargs = {
        "channel_profile_id": channel_id,
        "batch_key": "stable-batch-001",
        "producer": "orion",
        "source_type": "assistant",
        "batch_metadata": {"workflow": "daily_scan"},
        "records": [_competitor_record()],
    }

    first = ingest_intelligence_batch(**kwargs)
    replay = ingest_intelligence_batch(**kwargs)

    assert replay.replayed is True
    assert replay.batch.id == first.batch.id
    assert replay.records[0].id == first.records[0].id
    assert replay.created_count == 1
    assert replay.updated_count == 0

    changed = dict(kwargs)
    changed["records"] = [_competitor_record(summary="Different content")]
    with pytest.raises(ValueError, match="different intelligence content"):
        ingest_intelligence_batch(**changed)

    with intelligence_scope() as session:
        assert session.scalar(
            select(func.count()).select_from(IntelligenceIngestBatch)
        ) == 1
        assert session.scalar(
            select(func.count()).select_from(IntelligenceRecord)
        ) == 1
        assert session.scalar(
            select(func.count()).select_from(IntelligenceBatchRecord)
        ) == 1


def test_new_batch_updates_stable_record_without_losing_audit_history(
    intelligence_scope,
) -> None:
    channel_id = _create_channel(
        intelligence_scope,
        suffix="update",
        title="Update Test",
    )
    first = ingest_intelligence_batch(
        channel_profile_id=channel_id,
        batch_key="research-001",
        producer="orion",
        source_type="assistant",
        records=[_competitor_record(summary="Initial observation")],
    )
    second_record = _competitor_record(summary="Updated after a fresh scan")
    second_record["tags"] = ["competitor", "gaming_news", "watch"]
    second = ingest_intelligence_batch(
        channel_profile_id=channel_id,
        batch_key="research-002",
        producer="orion",
        source_type="assistant",
        records=[second_record],
    )

    assert second.created_count == 0
    assert second.updated_count == 1
    assert second.records[0].id == first.records[0].id
    assert second.records[0].first_batch_id == first.batch.id
    assert second.records[0].last_batch_id == second.batch.id
    assert second.records[0].summary == "Updated after a fresh scan"
    assert second.records[0].tags == ["competitor", "gaming_news", "watch"]

    with intelligence_scope() as session:
        memberships = list(
            session.scalars(
                select(IntelligenceBatchRecord).order_by(
                    IntelligenceBatchRecord.created_at,
                    IntelligenceBatchRecord.ordinal,
                )
            )
        )
        assert [item.action for item in memberships] == ["created", "updated"]


def test_intelligence_records_cannot_cross_channel_boundaries(intelligence_scope) -> None:
    rank_snaxx = _create_channel(
        intelligence_scope,
        suffix="rank",
        title="RankSnaxx",
    )
    other_channel = _create_channel(
        intelligence_scope,
        suffix="other",
        title="Other Channel",
    )
    result = ingest_intelligence_batch(
        channel_profile_id=rank_snaxx,
        batch_key="rank-only-001",
        producer="orion",
        source_type="assistant",
        records=[_competitor_record()],
    )
    record_id = result.records[0].id

    assert list_intelligence_records(other_channel) == []
    with pytest.raises(ValueError, match="intelligence record not found"):
        get_intelligence_record(other_channel, record_id)
    with pytest.raises(ValueError, match="different channel"):
        resolve_command_resources(
            other_channel,
            [("intelligence_record", record_id)],
        )


def test_intelligence_batch_rejects_duplicate_record_identity(intelligence_scope) -> None:
    channel_id = _create_channel(
        intelligence_scope,
        suffix="dupe",
        title="Duplicate Test",
    )
    with pytest.raises(ValueError, match="duplicate record identities"):
        ingest_intelligence_batch(
            channel_profile_id=channel_id,
            batch_key="duplicate-001",
            producer="orion",
            source_type="assistant",
            records=[_competitor_record(), _competitor_record()],
        )
