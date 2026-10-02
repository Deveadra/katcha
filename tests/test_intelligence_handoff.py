from __future__ import annotations

import io
import json
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from katcha import db
from katcha.acquisition_models import (
    DiscoveryCandidate,
    IntelligenceIngestBatch,
    IntelligenceRecord,
    RightsAssessment,
)
from katcha.intelligence_models import ChannelProfile
from katcha.models import SourceItem
from katcha.publishing_models import YouTubeConnection
from katcha.services.intelligence_handoff import (
    handoff_inbox_summary,
    list_handoff_inbox,
    process_handoff_file,
    process_handoff_inbox,
    submit_handoff_file,
)


@pytest.fixture()
def handoff_scope(monkeypatch: pytest.MonkeyPatch):
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


def _create_channel(scope) -> uuid.UUID:
    connection_id = uuid.uuid4()
    channel_profile_id = uuid.uuid4()
    with scope() as session:
        session.add(
            YouTubeConnection(
                id=connection_id,
                channel_id="UC" + "h" * 22,
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
                id=channel_profile_id,
                youtube_connection_id=connection_id,
                status="active",
                timezone="America/Chicago",
            )
        )
    return channel_profile_id


def _batch(channel_profile_id: uuid.UUID, *, batch_key: str = "orion-first-run") -> dict:
    return {
        "channel_profile_id": str(channel_profile_id),
        "batch_key": batch_key,
        "producer": "orion",
        "source_type": "assistant",
        "batch_metadata": {"purpose": "handoff-test"},
        "records": [
            {
                "record_kind": "video",
                "record_key": "youtube:video:test123",
                "title": "Viral gaming clip",
                "summary": "Candidate for RankSnaxx review.",
                "source_url": "https://www.youtube.com/watch?v=test123",
                "platform": "youtube",
                "status": "active",
                "tags": ["viral", "review"],
                "payload": {"lane": "immediate_post_candidate"},
                "provenance": {
                    "collector": "orion",
                    "method": "web_research",
                    "confidence": 0.9,
                },
                "observed_at": "2026-10-01T14:00:00Z",
            }
        ],
    }


def _encoded(payload: dict) -> bytes:
    return json.dumps(payload, sort_keys=True).encode("utf-8")


def test_handoff_file_imports_through_existing_ingestion_service(
    handoff_scope,
    tmp_path: Path,
) -> None:
    channel_id = _create_channel(handoff_scope)
    root = tmp_path / "handoff"
    content = _encoded(_batch(channel_id))

    queued = submit_handoff_file("rank-snaxx-001.json", content, root=root)
    assert queued.status == "incoming"

    processed = process_handoff_file("rank-snaxx-001.json", root=root)

    assert processed.status == "processed"
    assert processed.channel_profile_id == str(channel_id)
    assert processed.batch_key == "orion-first-run"
    assert processed.record_count == 1
    assert processed.receipt is not None
    assert processed.receipt["created_count"] == 1
    assert processed.receipt["updated_count"] == 0
    assert (root / "processed" / "rank-snaxx-001.json").exists()
    assert not (root / "incoming" / "rank-snaxx-001.json").exists()
    assert (root / "receipts" / "rank-snaxx-001.json.receipt.json").exists()

    with handoff_scope() as session:
        assert session.scalar(
            select(func.count()).select_from(IntelligenceIngestBatch)
        ) == 1
        record = session.scalar(select(IntelligenceRecord))
        assert record is not None
        assert record.channel_profile_id == channel_id
        assert record.record_key == "youtube:video:test123"


def test_same_handoff_upload_is_idempotent_after_processing(
    handoff_scope,
    tmp_path: Path,
) -> None:
    channel_id = _create_channel(handoff_scope)
    root = tmp_path / "handoff"
    content = _encoded(_batch(channel_id))

    submit_handoff_file("stable.json", content, root=root)
    first = process_handoff_file("stable.json", root=root)
    duplicate = submit_handoff_file("stable.json", content, root=root)

    assert first.status == "processed"
    assert duplicate.status == "processed"
    assert duplicate.receipt == first.receipt

    with handoff_scope() as session:
        assert session.scalar(
            select(func.count()).select_from(IntelligenceIngestBatch)
        ) == 1
        assert session.scalar(
            select(func.count()).select_from(IntelligenceRecord)
        ) == 1


def test_same_filename_with_different_content_fails_closed(
    handoff_scope,
    tmp_path: Path,
) -> None:
    channel_id = _create_channel(handoff_scope)
    root = tmp_path / "handoff"
    content = _encoded(_batch(channel_id))
    submit_handoff_file("stable.json", content, root=root)

    changed = _batch(channel_id)
    changed["records"][0]["summary"] = "Different payload"

    with pytest.raises(ValueError, match="different content"):
        submit_handoff_file("stable.json", _encoded(changed), root=root)


def test_invalid_handoff_is_retained_in_failed_queue(
    handoff_scope,
    tmp_path: Path,
) -> None:
    root = tmp_path / "handoff"
    submit_handoff_file("broken.json", b'{"records": []}', root=root)

    result = process_handoff_file("broken.json", root=root)

    assert result.status == "failed"
    assert result.error is not None
    assert "failed validation" in result.error
    assert (root / "failed" / "broken.json").exists()
    assert not (root / "incoming" / "broken.json").exists()
    receipt = json.loads(
        (root / "receipts" / "broken.json.receipt.json").read_text(encoding="utf-8")
    )
    assert receipt["status"] == "failed"
    assert receipt["error"]


def test_handoff_filename_rejects_path_traversal(
    handoff_scope,
    tmp_path: Path,
) -> None:
    channel_id = _create_channel(handoff_scope)
    with pytest.raises(ValueError, match="simple .json name"):
        submit_handoff_file(
            "../outside.json",
            _encoded(_batch(channel_id)),
            root=tmp_path / "handoff",
        )


def test_folder_scan_processes_manually_dropped_files(
    handoff_scope,
    tmp_path: Path,
) -> None:
    channel_id = _create_channel(handoff_scope)
    root = tmp_path / "handoff"
    summary = handoff_inbox_summary(root=root)
    assert summary["counts"] == {"incoming": 0, "processed": 0, "failed": 0}

    incoming = root / "incoming"
    (incoming / "drop-a.json").write_bytes(
        _encoded(_batch(channel_id, batch_key="drop-a"))
    )
    second = _batch(channel_id, batch_key="drop-b")
    second["records"][0]["record_key"] = "youtube:video:test456"
    (incoming / "drop-b.json").write_bytes(_encoded(second))

    results = process_handoff_inbox(root=root)

    assert [item.status for item in results] == ["processed", "processed"]
    assert {item.batch_key for item in results} == {"drop-a", "drop-b"}
    items = list_handoff_inbox(root=root)
    assert items == []
    assert handoff_inbox_summary(root=root)["counts"]["processed"] == 2

    with handoff_scope() as session:
        assert session.scalar(
            select(func.count()).select_from(IntelligenceIngestBatch)
        ) == 2
        assert session.scalar(
            select(func.count()).select_from(IntelligenceRecord)
        ) == 2


def test_different_file_with_conflicting_batch_key_moves_to_failed(
    handoff_scope,
    tmp_path: Path,
) -> None:
    channel_id = _create_channel(handoff_scope)
    root = tmp_path / "handoff"
    original = _batch(channel_id, batch_key="conflict-key")
    submit_handoff_file("first.json", _encoded(original), root=root)
    assert process_handoff_file("first.json", root=root).status == "processed"

    changed = _batch(channel_id, batch_key="conflict-key")
    changed["records"][0]["summary"] = "Conflicting content"
    submit_handoff_file("second.json", _encoded(changed), root=root)
    result = process_handoff_file("second.json", root=root)

    assert result.status == "failed"
    assert result.error is not None
    assert "different intelligence content" in result.error
    assert (root / "failed" / "second.json").exists()


@pytest.mark.asyncio
async def test_handoff_api_upload_processes_file(
    handoff_scope,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    from starlette.datastructures import UploadFile

    from katcha.api import acquisition
    from katcha.services import intelligence_handoff

    channel_id = _create_channel(handoff_scope)
    root = tmp_path / "api-handoff"
    monkeypatch.setattr(
        intelligence_handoff,
        "get_settings",
        lambda: SimpleNamespace(intelligence_handoff_dir=root),
    )
    upload = UploadFile(
        filename="api-batch.json",
        file=io.BytesIO(_encoded(_batch(channel_id, batch_key="api-batch"))),
    )

    result = await acquisition.upload_intelligence_handoff_file(
        file=upload,
        process=True,
    )

    assert result.status == "processed"
    assert result.channel_profile_id == str(channel_id)
    assert result.batch_key == "api-batch"
    inbox = acquisition.get_intelligence_handoff_inbox(limit=100)
    assert inbox.counts["processed"] == 1
    assert inbox.items == []


def test_same_filename_new_revision_archives_without_overwriting(
    handoff_scope,
    tmp_path: Path,
) -> None:
    channel_id = _create_channel(handoff_scope)
    root = tmp_path / "handoff"
    first_payload = _batch(channel_id, batch_key="revision-v1")
    first_content = _encoded(first_payload)
    submit_handoff_file("visionquest.json", first_content, root=root)
    first = process_handoff_file("visionquest.json", root=root)
    assert first.status == "processed"

    second_payload = _batch(channel_id, batch_key="revision-v2")
    second_payload["records"][0]["record_key"] = "youtube:video:visionquest-v2"
    second_content = _encoded(second_payload)
    queued = submit_handoff_file("visionquest.json", second_content, root=root)
    assert queued.status == "incoming"
    second = process_handoff_file("visionquest.json", root=root)
    assert second.status == "processed"

    archived = sorted((root / "processed").glob("visionquest*.json"))
    assert len(archived) == 2
    assert archived[0].read_bytes() != archived[1].read_bytes()
    assert list_handoff_inbox(root=root) == []
    assert handoff_inbox_summary(root=root)["counts"]["processed"] == 2


@pytest.mark.asyncio
async def test_api_startup_drains_pending_handoffs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from katcha.api import main

    called: dict[str, int] = {}

    def fake_process_handoff_inbox(*, limit: int = 50):
        called["limit"] = limit
        return []

    async def fake_reconcile(*, limit: int = 200):
        called["reconcile_limit"] = limit
        return []

    monkeypatch.setattr(main, "process_handoff_inbox", fake_process_handoff_inbox)
    monkeypatch.setattr(main, "reconcile_authorized_handoff_records", fake_reconcile)

    await main._process_pending_handoffs_on_startup()

    assert called == {"limit": 50, "reconcile_limit": 200}


@pytest.mark.asyncio
async def test_authorized_official_trailer_handoff_queues_acquisition(
    handoff_scope,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from katcha.services import intelligence_automation

    channel_id = _create_channel(handoff_scope)
    payload = _batch(channel_id, batch_key="visionquest-v2")
    payload["records"][0] = {
        "record_kind": "video",
        "record_key": "youtube:video:sXKnmgmbkoE",
        "title": "Marvel Television's VisionQuest | Official Trailer",
        "summary": "Urgent official trailer.",
        "source_url": "https://www.youtube.com/watch?v=sXKnmgmbkoE",
        "platform": "youtube",
        "status": "active",
        "tags": ["visionquest", "official_trailer"],
        "payload": {
            "creator": "Marvel Entertainment",
            "creator_url": "https://www.youtube.com/@marvel",
            "production_intent": "source_passthrough",
            "operator_authorized": True,
            "authorization_scope": "official_trailer_repost",
            "official_source_verified": True,
            "preupload_packaging_required": True,
        },
        "provenance": {
            "collector": "orion",
            "confidence": 0.99,
            "official_channel_verified": True,
        },
        "observed_at": "2026-10-02T10:45:00Z",
    }
    root = tmp_path / "handoff"
    submit_handoff_file("visionquest.json", _encoded(payload), root=root)
    processed = process_handoff_file("visionquest.json", root=root)

    started: list[tuple[str, str]] = []

    async def fake_start(source_id: str, workflow_id: str) -> str:
        started.append((source_id, workflow_id))
        return workflow_id

    monkeypatch.setattr(intelligence_automation, "start_ingest_workflow", fake_start)

    results = await intelligence_automation.advance_processed_handoff_receipt(
        processed.receipt
    )

    assert len(results) == 1
    assert results[0].action == "ingest_queued"
    assert results[0].candidate_id is not None
    assert results[0].source_id is not None
    assert started == [(str(results[0].source_id), str(results[0].workflow_id))]

    with handoff_scope() as session:
        candidate = session.get(DiscoveryCandidate, results[0].candidate_id)
        assert candidate is not None
        assert candidate.status == "promoted"
        assert candidate.candidate_metadata["operator_authorized"] is True
        assessment = session.scalar(
            select(RightsAssessment).where(
                RightsAssessment.discovery_candidate_id == candidate.id
            )
        )
        assert assessment is not None
        assert assessment.production_eligible is True
        source = session.get(SourceItem, results[0].source_id)
        assert source is not None
        assert source.status == "registered"
        assert source.source_metadata["channel_profile_id"] == str(channel_id)
        assert source.source_metadata["intelligence_record_id"]


@pytest.mark.asyncio
async def test_startup_reconciles_previously_stored_authorized_handoff(
    handoff_scope,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from katcha.services import intelligence_automation

    channel_id = _create_channel(handoff_scope)
    result = ingest_intelligence_batch(
        channel_profile_id=channel_id,
        batch_key="stored-before-automation",
        producer="orion",
        source_type="assistant",
        records=[
            {
                "record_kind": "video",
                "record_key": "youtube:video:stored-trailer",
                "title": "Stored Official Trailer",
                "summary": "Already ingested intelligence awaiting automation.",
                "source_url": "https://www.youtube.com/watch?v=stored-trailer",
                "platform": "youtube",
                "tags": ["official_trailer"],
                "payload": {
                    "creator": "Official Studio",
                    "production_intent": "source_passthrough",
                    "operator_authorized": True,
                    "authorization_scope": "official_trailer_repost",
                    "official_source_verified": True,
                    "preupload_packaging_required": True,
                },
                "provenance": {
                    "collector": "orion",
                    "confidence": 1.0,
                    "official_channel_verified": True,
                },
                "observed_at": "2026-10-02T10:45:00Z",
            }
        ],
    )
    assert result.records

    started: list[tuple[str, str]] = []

    async def fake_start(source_id: str, workflow_id: str) -> str:
        started.append((source_id, workflow_id))
        return workflow_id

    monkeypatch.setattr(intelligence_automation, "start_ingest_workflow", fake_start)

    reconciled = await intelligence_automation.reconcile_authorized_handoff_records()

    assert len(reconciled) == 1
    assert reconciled[0].record_id == result.records[0].id
    assert reconciled[0].action == "ingest_queued"
    assert started == [
        (str(reconciled[0].source_id), str(reconciled[0].workflow_id))
    ]


def test_symlinked_local_drop_is_not_processed(
    handoff_scope,
    tmp_path: Path,
) -> None:
    channel_id = _create_channel(handoff_scope)
    root = tmp_path / "handoff"
    handoff_inbox_summary(root=root)
    outside = tmp_path / "outside.json"
    outside.write_bytes(_encoded(_batch(channel_id, batch_key="outside")))
    link = root / "incoming" / "linked.json"
    link.symlink_to(outside)

    assert process_handoff_inbox(root=root) == []
    assert all(item.filename != "linked.json" for item in list_handoff_inbox(root=root))
