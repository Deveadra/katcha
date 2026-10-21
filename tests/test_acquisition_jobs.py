"""Offline control-plane tests; no provider, browser, or R2 acceptance implied."""

from __future__ import annotations

import json
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from katcha import db
from katcha.acquisition.job_contracts import (
    ClaimJob,
    EnqueueJob,
    FailJob,
    HeartbeatJob,
    ReportJob,
)
from katcha.acquisition_job_models import AcquisitionJob, AcquisitionTransfer
from katcha.acquisition_models import DiscoveryCandidate
from katcha.editorial.media_intake.contracts import ProviderPermissionV1
from katcha.editorial.media_intake.receipts import claim_acquisition, start_acquisition
from katcha.editorial_models import AcquisitionReceipt, MediaAssetValidation
from katcha.intelligence_models import ChannelProfile
from katcha.models import Clip, DomainEvent, SourceItem
from katcha.services import acquisition_jobs as jobs


@pytest.fixture
def job_db(monkeypatch, tmp_path):
    db.load_model_metadata()
    engine = create_engine(
        f"sqlite:///{tmp_path / 'jobs.sqlite'}", connect_args={"check_same_thread": False}
    )
    db.Base.metadata.create_all(
        engine,
        tables=[
            ChannelProfile.__table__,
            DiscoveryCandidate.__table__,
            AcquisitionReceipt.__table__,
            AcquisitionJob.__table__,
            DomainEvent.__table__,
            SourceItem.__table__,
            Clip.__table__,
            AcquisitionTransfer.__table__,
            MediaAssetValidation.__table__,
        ],
    )
    monkeypatch.setattr(db, "SessionLocal", sessionmaker(bind=engine, expire_on_commit=False))
    channel, other, candidate = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    with db.session_scope() as session:
        session.add_all(
            [
                ChannelProfile(id=channel, youtube_connection_id=uuid.uuid4()),
                ChannelProfile(id=other, youtube_connection_id=uuid.uuid4()),
                DiscoveryCandidate(
                    id=candidate,
                    adapter_key="licensed",
                    platform="web",
                    source_url="https://media.example/item",
                    canonical_url="https://media.example/item",
                    candidate_metadata={"channel_profile_id": str(channel)},
                ),
            ]
        )
    receipt = start_acquisition(
        channel,
        candidate,
        operation_key="approved-transfer",
        permission=ProviderPermissionV1(
            provider="licensed",
            operations={"download"},
            allowed_hosts=["media.example"],
            evidence_scope="operator supplied licensed media",
            evidence_refs=["permission:fixture"],
            expires_at=datetime.now(UTC) + timedelta(days=1),
        ),
        fetch_url_if_allowed="https://media.example/item?token=sensitive-fixture",
        actor="fixture",
    )
    yield channel, other, receipt.id
    engine.dispose()


def queued(fixture, *, max_bytes=4 * 1024**3):
    channel, _, receipt_id = fixture
    return jobs.enqueue_job(channel, EnqueueJob(receipt_id=receipt_id, max_bytes=max_bytes))


def leased(fixture):
    row = queued(fixture)
    message = ClaimJob(claim_id=uuid.uuid4())
    row, _, _ = jobs.claim_job(fixture[0], row.id, "worker-a", message)
    return row, {"claim_id": message.claim_id, "generation": row.generation}


def expire(row):
    with db.session_scope() as session:
        session.get(AcquisitionJob, row.id).lease_expires_at = datetime.now(UTC) - timedelta(
            seconds=1
        )


def receipt_status(receipt_id):
    with db.session_scope() as session:
        return session.get(AcquisitionReceipt, receipt_id).status


def test_enqueue_reserves_existing_receipt_and_replays_identical_request(job_db):
    channel, _, receipt_id = job_db
    row = queued(job_db)
    assert queued(job_db).id == row.id
    assert receipt_status(receipt_id) == "acquiring"
    assert row.status == "queued" and row.generation == 0
    with pytest.raises(jobs.JobConflict, match="byte limit"):
        queued(job_db, max_bytes=1024)
    with pytest.raises(ValueError, match="reconcile"):
        claim_acquisition(channel, receipt_id)
    with db.session_scope() as session:
        assert len(list(session.scalars(select(AcquisitionJob)))) == 1
        events = list(
            session.scalars(
                select(DomainEvent).where(
                    DomainEvent.aggregate_type == "acquisition_job",
                )
            )
        )
        assert len(events) == 1
        assert "sensitive-fixture" not in json.dumps([event.payload for event in events])


@pytest.mark.parametrize("change", ["expired", "no_hosts", "origin", "channel", "provider"])
def test_enqueue_rechecks_permission_and_channel_ownership(job_db, change):
    channel, other, receipt_id = job_db
    with db.session_scope() as session:
        receipt = session.get(AcquisitionReceipt, receipt_id)
        scope = dict(receipt.authorization_scope)
        if change == "expired":
            scope["expires_at"] = (datetime.now(UTC) - timedelta(days=1)).isoformat()
        elif change == "no_hosts":
            scope["allowed_hosts"] = []
        elif change == "origin":
            receipt.fetch_url_if_allowed = "https://127.0.0.1/internal"
            scope["allowed_hosts"] = ["127.0.0.1"]
        elif change == "channel":
            session.get(DiscoveryCandidate, receipt.candidate_id).candidate_metadata = {
                "channel_profile_id": str(other),
            }
        elif change == "provider":
            scope["provider"] = "different"
        receipt.authorization_scope = scope
    with pytest.raises(jobs.JobConflict, match="permission"):
        queued(job_db)
    assert receipt_status(receipt_id) == "authorized"
    assert jobs.list_jobs(channel) == []


@pytest.mark.parametrize("status", ["acquiring", "failed", "unknown", "succeeded"])
def test_existing_active_final_or_unknown_receipt_cannot_be_enqueued(job_db, status):
    with db.session_scope() as session:
        receipt = session.get(AcquisitionReceipt, job_db[2])
        receipt.status = status
        receipt.error_category = "fixture"
        if status == "succeeded":
            receipt.sha256, receipt.object_store_key = "a" * 64, "canonical/fixture"
    with pytest.raises(jobs.JobConflict, match="reconcile"):
        queued(job_db)


def test_claim_retry_preserves_lease_but_another_process_cannot_claim(job_db):
    channel = job_db[0]
    row, identity = leased(job_db)
    again, url, permission = jobs.claim_job(
        channel,
        row.id,
        "worker-a",
        ClaimJob(
            claim_id=identity["claim_id"],
        ),
    )
    assert again.lease_expires_at == row.lease_expires_at
    assert again.generation == 1
    assert permission.allows_host("media.example")
    assert url.endswith("token=sensitive-fixture")
    assert jobs.staging_key(row) == f"acquisition-staging/{channel}/{row.id}/1/media"
    for worker, claim_id in [("worker-b", identity["claim_id"]), ("worker-a", uuid.uuid4())]:
        with pytest.raises(jobs.JobConflict):
            jobs.claim_job(channel, row.id, worker, ClaimJob(claim_id=claim_id))


def test_heartbeat_persists_large_progress_and_duplicate_does_not_renew(job_db):
    row, identity = leased(job_db)
    message = HeartbeatJob(**identity, sequence=1, bytes_received=3 * 1024**3, phase="uploading")
    first = jobs.heartbeat_job(job_db[0], row.id, "worker-a", message)
    duplicate = jobs.heartbeat_job(job_db[0], row.id, "worker-a", message)
    assert duplicate.bytes_received == 3 * 1024**3
    assert duplicate.lease_expires_at == first.lease_expires_at
    for invalid in [
        message.model_copy(update={"bytes_received": 100}),
        message.model_copy(update={"sequence": 2, "bytes_received": 100}),
        message.model_copy(update={"sequence": 2, "bytes_received": 5 * 1024**3}),
        message.model_copy(update={"generation": 2}),
    ]:
        with pytest.raises(jobs.JobConflict):
            jobs.heartbeat_job(job_db[0], row.id, "worker-a", invalid)


@pytest.mark.parametrize("action", ["claim", "heartbeat", "report", "fail"])
def test_expired_lease_rejects_all_execution_updates(job_db, action):
    row, identity = leased(job_db)
    expire(row)
    with pytest.raises(jobs.JobConflict):
        if action == "claim":
            jobs.claim_job(job_db[0], row.id, "worker-a", ClaimJob(claim_id=identity["claim_id"]))
        elif action == "heartbeat":
            jobs.heartbeat_job(
                job_db[0],
                row.id,
                "worker-a",
                HeartbeatJob(
                    **identity,
                    sequence=1,
                    bytes_received=0,
                    phase="acquiring",
                ),
            )
        elif action == "report":
            jobs.report_job(
                job_db[0],
                row.id,
                "worker-a",
                ReportJob(
                    **identity,
                    sha256="a" * 64,
                    size_bytes=10,
                ),
            )
        else:
            jobs.fail_job(job_db[0], row.id, "worker-a", FailJob(**identity, code="access_denied"))


def test_reconcile_quarantines_unknown_outcome_and_never_retries(job_db):
    row, identity = leased(job_db)
    with pytest.raises(jobs.JobConflict):
        jobs.reconcile_expired_job(job_db[0], row.id)
    expire(row)
    lost = jobs.reconcile_expired_job(job_db[0], row.id)
    assert lost.status == "recovery_required" and lost.generation == 2
    assert receipt_status(row.receipt_id) == "unknown"
    assert jobs.reconcile_expired_job(job_db[0], row.id).id == row.id
    assert queued(job_db).status == "recovery_required"
    with pytest.raises(jobs.JobConflict):
        jobs.claim_job(job_db[0], row.id, "replacement", ClaimJob(claim_id=uuid.uuid4()))


def test_remote_report_is_unverified_and_does_not_promote_receipt(job_db):
    row, identity = leased(job_db)
    message = ReportJob(**identity, sha256="a" * 64, size_bytes=3 * 1024**3)
    reported = jobs.report_job(job_db[0], row.id, "worker-a", message)
    assert reported.status == "awaiting_validation"
    assert reported.pending_result == {
        "sha256": "a" * 64,
        "size_bytes": 3 * 1024**3,
        "object_store_key": jobs.staging_key(row),
    }
    assert receipt_status(row.receipt_id) == "acquiring"
    assert jobs.report_job(job_db[0], row.id, "worker-a", message).id == row.id
    with pytest.raises(jobs.JobConflict):
        jobs.report_job(
            job_db[0],
            row.id,
            "worker-a",
            message.model_copy(
                update={"sha256": "b" * 64},
            ),
        )
    cancelled = jobs.cancel_job(job_db[0], row.id)
    assert cancelled.pending_result["object_store_key"] == jobs.staging_key(row)
    assert cancelled.pending_result["object_store_key"] != jobs.staging_key(cancelled)


@pytest.mark.parametrize("was_leased", [False, True])
def test_cancel_fences_late_results_and_is_idempotent(job_db, was_leased):
    if was_leased:
        row, identity = leased(job_db)
    else:
        row = queued(job_db)
        identity = {"generation": 1, "claim_id": uuid.uuid4()}
    cancelled = jobs.cancel_job(job_db[0], row.id)
    assert cancelled.status == "cancelled"
    assert jobs.cancel_job(job_db[0], row.id).generation == cancelled.generation
    assert receipt_status(row.receipt_id) == ("unknown" if was_leased else "failed")
    with pytest.raises(jobs.JobConflict):
        jobs.report_job(
            job_db[0],
            row.id,
            "worker-a",
            ReportJob(
                **identity,
                sha256="a" * 64,
                size_bytes=100,
            ),
        )


@pytest.mark.parametrize(
    "code,expected",
    [
        ("authentication_required", "failed"),
        ("rate_limited", "failed"),
        ("outcome_unknown", "unknown"),
        ("transport_error", "unknown"),
    ],
)
def test_failures_have_bounded_codes_and_preserve_uncertainty(job_db, code, expected):
    row, identity = leased(job_db)
    message = FailJob(**identity, code=code)
    failed = jobs.fail_job(job_db[0], row.id, "worker-a", message)
    assert receipt_status(row.receipt_id) == expected
    assert failed.failure_code == code
    assert jobs.fail_job(job_db[0], row.id, "worker-a", message).id == row.id


def test_permission_expiration_after_enqueue_blocks_claim_and_after_claim_blocks_report(job_db):
    row = queued(job_db)
    with db.session_scope() as session:
        receipt = session.get(AcquisitionReceipt, row.receipt_id)
        scope = dict(receipt.authorization_scope)
        original = scope["expires_at"]
        scope["expires_at"] = "2000-01-01T00:00:00Z"
        receipt.authorization_scope = scope
    with pytest.raises(jobs.JobConflict):
        jobs.claim_job(job_db[0], row.id, "worker-a", ClaimJob(claim_id=uuid.uuid4()))
    with db.session_scope() as session:
        receipt = session.get(AcquisitionReceipt, row.receipt_id)
        receipt.authorization_scope = {**receipt.authorization_scope, "expires_at": original}
    row, identity = leased(job_db)
    with db.session_scope() as session:
        receipt = session.get(AcquisitionReceipt, row.receipt_id)
        receipt.authorization_scope = {
            **receipt.authorization_scope,
            "expires_at": "2000-01-01T00:00:00Z",
        }
    with pytest.raises(jobs.JobConflict):
        jobs.report_job(
            job_db[0],
            row.id,
            "worker-a",
            ReportJob(
                **identity,
                sha256="a" * 64,
                size_bytes=10,
            ),
        )


def test_cross_channel_lookup_and_mutation_are_unavailable(job_db):
    row, identity = leased(job_db)
    for action in [
        lambda: jobs.get_job(job_db[1], row.id),
        lambda: jobs.cancel_job(job_db[1], row.id),
        lambda: jobs.claim_job(job_db[1], row.id, "worker-a", ClaimJob(claim_id=uuid.uuid4())),
        lambda: jobs.enqueue_job(job_db[1], EnqueueJob(receipt_id=row.receipt_id)),
    ]:
        with pytest.raises(jobs.JobNotFound):
            action()
    assert jobs.list_jobs(job_db[1]) == []


@pytest.mark.parametrize("operation", ["enqueue", "claim", "report", "cancel"])
def test_concurrent_transitions_have_one_owner_and_one_event(job_db, operation):
    if operation == "enqueue":
        row = None
    elif operation == "report":
        row, identity = leased(job_db)
    else:
        row = queued(job_db)

    def transition():
        try:
            if operation == "enqueue":
                return queued(job_db).id
            if operation == "claim":
                return jobs.claim_job(
                    job_db[0],
                    row.id,
                    "worker-a",
                    ClaimJob(
                        claim_id=uuid.uuid4(),
                    ),
                )[0].id
            if operation == "report":
                return jobs.report_job(
                    job_db[0],
                    row.id,
                    "worker-a",
                    ReportJob(
                        **identity,
                        sha256="a" * 64,
                        size_bytes=100,
                    ),
                ).id
            return jobs.cancel_job(job_db[0], row.id).id
        except jobs.JobConflict:
            return None

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: transition(), range(2)))
    accepted = [result for result in results if result is not None]
    assert accepted and len(set(accepted)) == 1
    row_id = accepted[0]
    if operation == "claim":
        assert results.count(None) == 1
    action = {
        "enqueue": "queued",
        "claim": "claimed",
        "report": "result_reported",
        "cancel": "cancelled",
    }[operation]
    with db.session_scope() as session:
        events = list(
            session.scalars(
                select(DomainEvent).where(
                    DomainEvent.aggregate_id == str(row_id),
                    DomainEvent.event_type == f"acquisition_job.{action}",
                )
            )
        )
        assert len(events) == 1


def test_messages_reject_arbitrary_credentials_urls_and_success_assertions():
    with pytest.raises(ValidationError):
        ReportJob(
            generation=1,
            claim_id=uuid.uuid4(),
            sha256="a" * 64,
            size_bytes=1,
            verified=True,
            object_store_key="other-channel/object",
            cookies="secret",
        )
    with pytest.raises(ValidationError):
        EnqueueJob(receipt_id=uuid.uuid4(), fetch_url="https://internal.example")


def test_cancel_does_not_classify_a_concurrent_claim_as_known_failure(job_db, monkeypatch):
    row = queued(job_db)
    original = jobs._change

    def concurrent_claim(session, channel, job_id, *conditions, **values):
        if values.get("status") == "cancelled":
            jobs.claim_job(channel, job_id, "worker-a", ClaimJob(claim_id=uuid.uuid4()))
        return original(session, channel, job_id, *conditions, **values)

    monkeypatch.setattr(jobs, "_change", concurrent_claim)
    with pytest.raises(jobs.JobConflict):
        jobs.cancel_job(job_db[0], row.id)
    assert jobs.get_job(job_db[0], row.id).status == "leased"
    assert receipt_status(row.receipt_id) == "acquiring"
    monkeypatch.setattr(jobs, "_change", original)
    assert jobs.cancel_job(job_db[0], row.id).status == "cancelled"
    assert receipt_status(row.receipt_id) == "unknown"


def test_receipt_conflict_rolls_back_job_cancellation(job_db):
    row, _ = leased(job_db)
    with db.session_scope() as session:
        receipt = session.get(AcquisitionReceipt, row.receipt_id)
        receipt.status, receipt.error_category = "unknown", "external_reconciliation"
    with pytest.raises(jobs.JobConflict, match="Receipt changed"):
        jobs.cancel_job(job_db[0], row.id)
    assert jobs.get_job(job_db[0], row.id).status == "leased"
    assert receipt_status(row.receipt_id) == "unknown"


def test_audit_failure_rolls_back_job_and_receipt_reservation(job_db, monkeypatch):
    def fail(*args):
        raise RuntimeError("audit persistence failed")

    monkeypatch.setattr(jobs, "_event", fail)
    with pytest.raises(RuntimeError, match="audit persistence"):
        queued(job_db)
    assert jobs.list_jobs(job_db[0]) == []
    assert receipt_status(job_db[2]) == "authorized"


@pytest.fixture
def job_client(job_db, monkeypatch):
    from fastapi import Depends, FastAPI
    from fastapi.testclient import TestClient

    from katcha.api import acquisition_jobs as api
    from katcha.api import control_auth
    from katcha.config import Settings

    channel, other, _ = job_db
    settings = Settings(
        _env_file=None,
        remote_acquisition_enabled=True,
        control_principals=[
            {
                "name": "operator",
                "token": "operator-fixture-token-001",
                "scopes": ["*"],
                "channel_profile_ids": [str(channel)],
            },
            {
                "name": "worker-a",
                "token": "worker-a-fixture-token-001",
                "scopes": ["acquisition:work"],
                "channel_profile_ids": [str(channel)],
            },
            {
                "name": "worker-b",
                "token": "worker-b-fixture-token-001",
                "scopes": ["acquisition:work"],
                "channel_profile_ids": [str(channel)],
            },
            {
                "name": "other-worker",
                "token": "other-worker-fixture-token",
                "scopes": ["acquisition:work"],
                "channel_profile_ids": [str(other)],
            },
            {
                "name": "broad-worker",
                "token": "broad-worker-fixture-token",
                "scopes": ["acquisition:work"],
                "channel_profile_ids": ["*"],
            },
            {
                "name": "mixed-worker",
                "token": "mixed-worker-fixture-token",
                "scopes": ["acquisition:work", "channels:read"],
                "channel_profile_ids": [str(channel)],
            },
        ],
    )
    monkeypatch.setattr(control_auth, "get_settings", lambda: settings)
    monkeypatch.setattr(api, "get_settings", lambda: settings)
    app = FastAPI(dependencies=[Depends(control_auth.require_control_token)])
    app.include_router(api.router)

    @app.get("/v1/channels")
    def unrelated():
        return []

    yield TestClient(app), settings, f"/v1/channels/{channel}/acquisition-jobs"


def headers(identity="operator"):
    token = {
        "operator": "operator-fixture-token-001",
        "worker-a": "worker-a-fixture-token-001",
        "worker-b": "worker-b-fixture-token-001",
        "other": "other-worker-fixture-token",
        "broad": "broad-worker-fixture-token",
        "mixed": "mixed-worker-fixture-token",
    }[identity]
    return {"Authorization": f"Bearer {token}"}


def test_api_complete_protocol_uses_authenticated_identity_and_hides_source_secrets(
    job_db,
    job_client,
):
    client, _, root = job_client
    queued_response = client.post(root, headers=headers(), json={"receipt_id": str(job_db[2])})
    assert queued_response.status_code == 200, queued_response.text
    row = queued_response.json()
    path = f"{root}/{row['id']}"
    identity = {"claim_id": str(uuid.uuid4())}
    response = client.post(f"{path}/claim", headers=headers("worker-a"), json=identity)
    assert response.status_code == 200, response.text
    lease = response.json()
    assert response.headers["cache-control"] == "no-store"
    assert "sensitive-fixture" in lease["fetch_url"]
    identity["generation"] = lease["job"]["generation"]
    heartbeat = {**identity, "sequence": 1, "bytes_received": 3 * 1024**3, "phase": "uploading"}
    assert (
        client.post(f"{path}/heartbeat", headers=headers("worker-a"), json=heartbeat).status_code
        == 200
    )
    result = client.post(
        f"{path}/report",
        headers=headers("worker-a"),
        json={
            **identity,
            "sha256": "a" * 64,
            "size_bytes": 3 * 1024**3,
        },
    )
    assert result.status_code == 200, result.text
    assert result.json()["media_verified"] is False
    assert result.json()["status"] == "awaiting_validation"
    for resource in [root, path]:
        read = client.get(resource, headers=headers())
        assert read.status_code == 200, read.text
        assert "sensitive-fixture" not in read.text
        assert "fetch_url" not in read.text
        assert "claim_id" not in read.text
    assert receipt_status(job_db[2]) == "acquiring"
    assert client.get(root + "?status=queued", headers=headers("worker-a")).json() == []
    assert len(client.get(root + "?status=awaiting_validation", headers=headers()).json()) == 1
    with db.session_scope() as session:
        created = session.scalar(
            select(DomainEvent).where(
                DomainEvent.event_type == "acquisition_job.queued",
            )
        )
        assert created.payload["actor"] == "control-principal:operator"


def test_api_default_off_allows_read_and_cancel_but_no_new_execution(job_db, job_client):
    client, settings, root = job_client
    row = queued(job_db)
    settings.remote_acquisition_enabled = False
    assert (
        client.post(root, headers=headers(), json={"receipt_id": str(job_db[2])}).status_code == 503
    )
    assert (
        client.post(
            f"{root}/{row.id}/claim",
            headers=headers("worker-a"),
            json={
                "claim_id": str(uuid.uuid4()),
            },
        ).status_code
        == 503
    )
    assert client.get(root, headers=headers()).status_code == 200
    assert client.post(f"{root}/{row.id}/cancel", headers=headers()).status_code == 200


@pytest.mark.parametrize("identity", [None, "operator", "other", "broad", "mixed"])
def test_api_claim_requires_dedicated_channel_scoped_worker(job_db, job_client, identity):
    client, _, root = job_client
    row = queued(job_db)
    response = client.post(
        f"{root}/{row.id}/claim",
        headers=headers(identity) if identity else {},
        json={"claim_id": str(uuid.uuid4())},
    )
    assert response.status_code == (401 if identity is None else 403), response.text
    assert jobs.get_job(job_db[0], row.id).status == "queued"


def test_api_worker_cannot_enqueue_cancel_or_access_other_routes(job_db, job_client):
    client, _, root = job_client
    row = queued(job_db)
    for suffix, body in [
        ("", {"receipt_id": str(job_db[2])}),
        (f"/{row.id}/cancel", {}),
        (f"/{row.id}/reconcile", {}),
    ]:
        response = client.post(root + suffix, headers=headers("worker-a"), json=body)
        assert response.status_code == 403, response.text
    assert client.get("/v1/channels", headers=headers("worker-a")).status_code == 403


def test_api_another_worker_cannot_report_or_heartbeat_an_existing_lease(job_db, job_client):
    client, _, root = job_client
    row, identity = leased(job_db)
    identity["claim_id"] = str(identity["claim_id"])
    assert (
        client.post(
            f"{root}/{row.id}/heartbeat",
            headers=headers("worker-b"),
            json={
                **identity,
                "sequence": 1,
                "bytes_received": 1,
                "phase": "acquiring",
            },
        ).status_code
        == 409
    )
    assert (
        client.post(
            f"{root}/{row.id}/report",
            headers=headers("worker-b"),
            json={
                **identity,
                "sha256": "a" * 64,
                "size_bytes": 1,
            },
        ).status_code
        == 409
    )


def test_job_migration_upgrade_and_downgrade_match_model(tmp_path):
    import importlib.util
    from pathlib import Path

    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import inspect

    path = Path("migrations/versions/0065_acquisition_jobs.py")
    spec = importlib.util.spec_from_file_location("job_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    engine = create_engine(f"sqlite:///{tmp_path / 'migration.sqlite'}")
    with engine.begin() as connection:
        module.op = Operations(MigrationContext.configure(connection))
        module.upgrade()
        inspector = inspect(connection)
        assert {column["name"] for column in inspector.get_columns("acquisition_jobs")} == set(
            AcquisitionJob.__table__.columns.keys()
        ) - {"media_type", "source_item_id"}
        foreign_keys = inspector.get_foreign_keys("acquisition_jobs")
        assert foreign_keys[0]["constrained_columns"] == ["receipt_id", "channel_profile_id"]
        module.downgrade()
        assert "acquisition_jobs" not in inspect(connection).get_table_names()
    engine.dispose()
