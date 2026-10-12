from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from katcha import db
from katcha.acquisition_models import DiscoveryCandidate
from katcha.editorial.media_intake.contracts import (
    AcquisitionReceiptV1,
    ProviderPermissionV1,
    acquisition_permission,
    production_eligibility,
)
from katcha.editorial.media_intake.receipts import record_outcome, start_acquisition
from katcha.intelligence_models import ChannelProfile


@pytest.fixture
def receipt_db(monkeypatch):
    db.load_model_metadata()
    engine = create_engine(
        "sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    db.Base.metadata.create_all(engine)
    monkeypatch.setattr(db, "SessionLocal", sessionmaker(bind=engine, expire_on_commit=False))
    channel_id, candidate_id = uuid.uuid4(), uuid.uuid4()
    with db.session_scope() as session:
        session.add(
            ChannelProfile(
                id=channel_id,
                youtube_connection_id=uuid.uuid4(),
                status="active",
                timezone="UTC",
                profile_metadata={},
            )
        )
        session.add(
            DiscoveryCandidate(
                id=candidate_id,
                adapter_key="licensed-provider",
                external_id=uuid.uuid4().hex,
                source_url="https://media.example/item",
                canonical_url=f"https://media.example/{candidate_id}",
                platform="web",
                candidate_metadata={"channel_profile_id": str(channel_id)},
            )
        )
    yield channel_id, candidate_id
    engine.dispose()


def permission(operations: set[str] | None = None) -> ProviderPermissionV1:
    return ProviderPermissionV1(
        provider="licensed-provider",
        operations=operations or {"metadata", "download"},
        evidence_scope="licensed media download for project use",
        evidence_refs=["operator-attestation:123"],
        expires_at=datetime.now(UTC) + timedelta(days=1),
    )


def test_access_and_download_permission_are_distinct_and_rights_gate_production():
    metadata_only = permission({"metadata"})
    assert metadata_only.allows("metadata")
    assert not metadata_only.allows("download")
    for state in ("not_assessed", "reference_only", "unknown", "expired", "revoked", "denied"):
        allowed, _ = production_eligibility(state)
        assert not allowed
    assert not acquisition_permission(metadata_only)[0]
    # Production rights do not grant provider download permission.
    assert production_eligibility("production_cleared")[0]


def test_expired_permission_cannot_start_download(receipt_db):
    channel_id, candidate_id = receipt_db
    expired = permission().model_copy(
        update={"expires_at": datetime.now(UTC) - timedelta(seconds=2)}
    )
    with pytest.raises(ValueError, match="permission"):
        start_acquisition(
            channel_id,
            candidate_id,
            operation_key="expired",
            permission=expired,
            fetch_url_if_allowed="https://media.example/item",
            actor="test",
        )


def test_durable_receipt_is_idempotent_channel_scoped_and_unknown_is_not_retried(receipt_db):
    channel_id, candidate_id = receipt_db
    kwargs = {
        "operation_key": "attempt-1",
        "permission": permission(),
        "fetch_url_if_allowed": "https://media.example/item",
        "actor": "test",
    }
    first = start_acquisition(channel_id, candidate_id, **kwargs)
    replay = start_acquisition(channel_id, candidate_id, **kwargs)
    assert replay.id == first.id
    assert first.status == "authorized"
    assert AcquisitionReceiptV1.model_validate(first).candidate_id == candidate_id

    unknown = record_outcome(
        channel_id,
        first.id,
        status="unknown",
        response_time=datetime.now(UTC),
        error_category="connection_lost_after_request",
    )
    assert unknown.status == "unknown"
    with pytest.raises(ValueError, match="reconcile"):
        start_acquisition(channel_id, candidate_id, **kwargs)
    with pytest.raises(ValueError, match="final"):
        record_outcome(
            channel_id,
            first.id,
            status="failed",
            response_time=datetime.now(UTC),
            error_category="timeout",
        )


def test_success_receipt_requires_durable_content_evidence():
    common = {
        "id": uuid.uuid4(),
        "candidate_id": uuid.uuid4(),
        "operation_key": "valid",
        "authorization_scope": {"evidence_scope": "authorized"},
        "status": "succeeded",
        "request_time": datetime.now(UTC),
    }
    with pytest.raises(ValueError, match="content hash"):
        AcquisitionReceiptV1.model_validate(common)
    valid = AcquisitionReceiptV1.model_validate(
        {**common, "sha256": "a" * 64, "object_store_key": "media/sha256/aaaa"}
    )
    assert valid.status == "succeeded"
