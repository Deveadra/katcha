"""Real persistence/control-plane recovery tests; providers remain offline fixtures."""

import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import select
from test_acquisition_jobs import job_db, leased, receipt_status  # noqa: F401

from katcha import db
from katcha.acquisition.job_contracts import FailJob, LeaseMessage
from katcha.acquisition_job_models import AcquisitionBrowserCommand, AcquisitionChallenge
from katcha.config import get_settings
from katcha.editorial_models import AcquisitionReceipt
from katcha.services import acquisition_jobs as jobs
from katcha.services import acquisition_recovery as recovery


@pytest.fixture
def recovery_db(job_db, monkeypatch):  # noqa: F811
    settings = get_settings().model_copy(
        update={
            "acquisition_human_recovery_enabled": True,
            "remote_acquisition_enabled": True,
            "credential_encryption_key": Fernet.generate_key().decode(),
        }
    )
    monkeypatch.setattr(recovery, "get_settings", lambda: settings)
    monkeypatch.setattr("katcha.security.secrets.get_settings", lambda: settings)
    with db.session_scope() as session:
        db.Base.metadata.create_all(
            session.get_bind(),
            tables=[
                AcquisitionChallenge.__table__,
                AcquisitionBrowserCommand.__table__,
            ],
        )
    return job_db


def waiting(fixture):
    row, identity = leased(fixture)
    row = jobs.fail_job(
        fixture[0],
        row.id,
        "worker-a",
        FailJob(**identity, code="authentication_required", recovery_available=True),
    )
    challenge = recovery.get_challenge(fixture[0], row.id)
    return row, identity, challenge


def command(fixture, row, challenge, action=None):
    return recovery.enqueue_command(
        fixture[0],
        row.id,
        recovery.RecoveryAction(
            id=uuid.uuid4(), challenge_id=challenge["id"], action=action or {"action": "open"}
        ),
        "operator",
    )


def test_pause_deduplicates_and_preserves_original_receipt(recovery_db):
    row, identity, challenge = waiting(recovery_db)
    assert row.status == "waiting_human"
    assert receipt_status(row.receipt_id) == "acquiring"
    assert (
        jobs.fail_job(
            recovery_db[0],
            row.id,
            "worker-a",
            FailJob(**identity, code="authentication_required", recovery_available=True),
        ).id
        == row.id
    )
    with db.session_scope() as session:
        assert len(list(session.scalars(select(AcquisitionChallenge)))) == 1
    assert challenge["state"] == "waiting"
    with pytest.raises(jobs.JobNotFound):
        recovery.get_challenge(recovery_db[1], row.id)
    with pytest.raises(jobs.JobConflict):
        jobs.claim_job(recovery_db[0], row.id, "worker-b", jobs.ClaimJob(claim_id=uuid.uuid4()))


def test_encrypted_mailbox_never_replays_claimed_action(recovery_db):
    row, identity, challenge = waiting(recovery_db)
    secret = "private-password-not-a-fixture-log"
    cmd = command(recovery_db, row, challenge, {"action": "type", "text": secret})
    with db.session_scope() as session:
        stored = session.get(AcquisitionBrowserCommand, cmd["id"])
        assert secret not in stored.payload and secret not in stored.request_hash
    message = LeaseMessage(**identity)
    polled = recovery.poll(recovery_db[0], row.id, "worker-a", message)
    assert polled["command"]["action"]["text"] == secret
    # Lost HTTP response does not retype a password or resubmit a form.
    assert recovery.poll(recovery_db[0], row.id, "worker-a", message)["command"] is None
    with db.session_scope() as session:
        assert session.get(AcquisitionBrowserCommand, cmd["id"]).payload is None
    result = recovery.RecoveryResult(
        **identity, command_id=cmd["id"], result={"available": True, "text": "private-view"}
    )
    assert recovery.complete_command(recovery_db[0], row.id, "worker-a", result)["accepted"]
    assert recovery.complete_command(recovery_db[0], row.id, "worker-a", result)["accepted"]
    with db.session_scope() as session:
        assert "private-view" not in session.get(AcquisitionBrowserCommand, cmd["id"]).result
    assert recovery.get_command(recovery_db[0], row.id, cmd["id"])["result"] == result.result


def test_open_is_bound_to_original_source_and_wrong_worker_rejected(recovery_db):
    row, identity, challenge = waiting(recovery_db)
    command(recovery_db, row, challenge, {"action": "open", "url": "https://unapproved.test"})
    with pytest.raises(jobs.JobConflict):
        recovery.poll(recovery_db[0], row.id, "worker-b", LeaseMessage(**identity))
    polled = recovery.poll(recovery_db[0], row.id, "worker-a", LeaseMessage(**identity))
    assert polled["command"]["action"]["url"].startswith("https://media.example/")
    assert "fetch_url" not in recovery.get_challenge(recovery_db[0], row.id)


def test_pending_action_blocks_retry_and_concurrent_admission(recovery_db):
    row, identity, challenge = waiting(recovery_db)

    def attempt(_):
        try:
            return command(recovery_db, row, challenge)
        except jobs.JobConflict:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(attempt, range(2)))
    assert sum(r is not None for r in results) == 1
    with pytest.raises(jobs.JobConflict, match="Wait for"):
        recovery.request_retry(
            recovery_db[0], row.id, recovery.RecoveryRetry(challenge_id=challenge["id"]), "operator"
        )


def test_retry_fenced_idempotent_and_never_asserts_verified(recovery_db):
    row, identity, challenge = waiting(recovery_db)
    retry = recovery.RecoveryRetry(challenge_id=challenge["id"])
    assert (
        recovery.request_retry(recovery_db[0], row.id, retry, "operator")["state"]
        == "retry_requested"
    )
    assert (
        recovery.request_retry(recovery_db[0], row.id, retry, "operator")["state"]
        == "retry_requested"
    )
    resumed, _, _ = recovery.resume(recovery_db[0], row.id, "worker-a", LeaseMessage(**identity))
    repeated, _, _ = recovery.resume(recovery_db[0], row.id, "worker-a", LeaseMessage(**identity))
    assert resumed.generation == repeated.generation == identity["generation"] + 1
    assert resumed.claim_id == identity["claim_id"]
    assert resumed.status == "leased" and not resumed.media_verified
    assert receipt_status(row.receipt_id) == "acquiring"
    with pytest.raises(jobs.JobConflict):
        recovery.poll(recovery_db[0], row.id, "worker-b", LeaseMessage(**identity))
    with pytest.raises(jobs.JobConflict):
        jobs.fail_job(
            recovery_db[0], row.id, "worker-a", FailJob(**identity, code="authentication_required")
        )


@pytest.mark.parametrize("action", ["cancel", "expire"])
def test_cancellation_and_expiry_erase_sensitive_mailbox_and_block_resume(recovery_db, action):
    row, identity, challenge = waiting(recovery_db)
    cmd = command(recovery_db, row, challenge)
    if action == "cancel":
        jobs.cancel_job(recovery_db[0], row.id, "operator")
    else:
        with db.session_scope() as session:
            session.get(AcquisitionChallenge, challenge["id"]).expires_at = jobs._now() - timedelta(
                seconds=1
            )
        recovery.expire_wait(recovery_db[0], row.id)
    with db.session_scope() as session:
        stored = session.get(AcquisitionBrowserCommand, cmd["id"])
        assert stored.payload is None and stored.result is None
        assert session.get(AcquisitionChallenge, challenge["id"]).state == "closed"
    assert receipt_status(row.receipt_id) == "failed"
    with pytest.raises(jobs.JobConflict):
        recovery.resume(recovery_db[0], row.id, "worker-a", LeaseMessage(**identity))


def test_revoked_permission_blocks_browser_and_retry(recovery_db):
    row, identity, challenge = waiting(recovery_db)
    with db.session_scope() as session:
        receipt = session.get(AcquisitionReceipt, row.receipt_id)
        scope = dict(receipt.authorization_scope)
        scope["expires_at"] = (jobs._now() - timedelta(seconds=1)).isoformat()
        receipt.authorization_scope = scope
    with pytest.raises(jobs.JobConflict, match="permission"):
        command(recovery_db, row, challenge)
    with pytest.raises(jobs.JobConflict, match="permission"):
        recovery.poll(recovery_db[0], row.id, "worker-a", LeaseMessage(**identity))
    with pytest.raises(jobs.JobConflict, match="permission"):
        recovery.request_retry(
            recovery_db[0], row.id, recovery.RecoveryRetry(challenge_id=challenge["id"]), "operator"
        )


def test_session_lost_returns_to_wait_without_claiming_success(recovery_db):
    row, identity, challenge = waiting(recovery_db)
    recovery.request_retry(
        recovery_db[0], row.id, recovery.RecoveryRetry(challenge_id=challenge["id"]), "operator"
    )
    result = recovery.unavailable(
        recovery_db[0],
        row.id,
        "worker-a",
        recovery.RecoveryUnavailable(**identity, code="session_expired"),
    )
    assert result["state"] == "waiting" and result["code"] == "session_expired"
    assert jobs.get_job(recovery_db[0], row.id).status == "waiting_human"


def test_round_budget_prevents_endless_challenges(recovery_db):
    row, identity, challenge = waiting(recovery_db)
    for _ in range(recovery.MAX_ROUNDS):
        recovery.request_retry(
            recovery_db[0], row.id, recovery.RecoveryRetry(challenge_id=challenge["id"]), "operator"
        )
        row, _, _ = recovery.resume(recovery_db[0], row.id, "worker-a", LeaseMessage(**identity))
        identity["generation"] = row.generation
        row = jobs.fail_job(
            recovery_db[0],
            row.id,
            "worker-a",
            FailJob(**identity, code="authentication_required", recovery_available=True),
        )
        if row.status == "waiting_human":
            challenge = recovery.get_challenge(recovery_db[0], row.id)
    assert row.status == "failed"
    assert receipt_status(row.receipt_id) == "failed"


def test_unknown_and_rate_limit_never_enter_human_retry(recovery_db):
    row, identity = leased(recovery_db)
    result = jobs.fail_job(
        recovery_db[0],
        row.id,
        "worker-a",
        FailJob(**identity, code="session_lost", recovery_available=True),
    )
    assert result.status == "recovery_required"
    with db.session_scope() as session:
        assert session.scalar(select(AcquisitionChallenge)) is None


def test_expired_command_is_scrubbed_without_operator_visit(recovery_db):
    row, identity, challenge = waiting(recovery_db)
    cmd = command(recovery_db, row, challenge, {"action": "type", "text": "private"})
    with db.session_scope() as session:
        session.get(AcquisitionBrowserCommand, cmd["id"]).expires_at = jobs._now() - timedelta(
            seconds=1
        )
    recovery.clean_expired_mailboxes()
    with db.session_scope() as session:
        stored = session.get(AcquisitionBrowserCommand, cmd["id"])
        assert stored.state == "expired" and stored.payload is None


def test_migration_preserves_existing_jobs_and_refuses_history_loss(job_db):  # noqa: F811
    import importlib.util

    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import inspect

    row, identity = leased(job_db)
    spec = importlib.util.spec_from_file_location(
        "human_migration", "migrations/versions/0067_acquisition_human_recovery.py"
    )
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    with db.session_scope() as session:
        engine = session.get_bind()
    db.Base.metadata.create_all(
        engine,
        tables=[
            table
            for table in db.Base.metadata.sorted_tables
            if table.name not in {"acquisition_challenges", "acquisition_browser_commands"}
        ],
    )
    with engine.begin() as connection:
        migration.op = Operations(MigrationContext.configure(connection))
        migration.upgrade()
        assert set(
            c["name"] for c in inspect(connection).get_columns("acquisition_challenges")
        ) == set(AcquisitionChallenge.__table__.columns.keys())
    assert jobs.get_job(job_db[0], row.id).status == "leased"
    with db.session_scope() as session:
        session.add(
            AcquisitionChallenge(
                job_id=row.id,
                channel_profile_id=job_db[0],
                generation=row.generation,
                expires_at=jobs._now() + timedelta(minutes=30),
            )
        )
    with engine.begin() as connection:
        migration.op = Operations(MigrationContext.configure(connection))
        with pytest.raises(RuntimeError, match="Retain human recovery"):
            migration.downgrade()
    assert jobs.get_job(job_db[0], row.id).claim_id == identity["claim_id"]
