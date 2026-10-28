"""Channel-scoped, encrypted human recovery mailbox for the acquisition host.

Human actions request a single retry; only the existing media validation pipeline
can declare success. No browser cookies ever cross this control-plane boundary.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import timedelta

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select, update

from katcha.acquisition.job_contracts import LeaseMessage
from katcha.acquisition_job_models import AcquisitionBrowserCommand, AcquisitionChallenge
from katcha.config import get_settings
from katcha.db import session_scope
from katcha.security.secrets import decrypt_secret, encrypt_secret
from katcha.services import acquisition_jobs as jobs
from katcha.services.server_browser import BrowserAction

WAIT_SECONDS = 1800
COMMAND_SECONDS = 90
MAX_ROUNDS = 3


class RecoveryAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: uuid.UUID
    challenge_id: uuid.UUID
    # BrowserAction validates all fields. 'open' URL is always replaced server-side.
    action: dict = Field(default_factory=dict)


class RecoveryRetry(BaseModel):
    model_config = ConfigDict(extra="forbid")
    challenge_id: uuid.UUID


class RecoveryResult(LeaseMessage):
    command_id: uuid.UUID
    result: dict


class RecoveryUnavailable(LeaseMessage):
    code: str = Field(pattern=r"^(session_expired|browser_unavailable)$")


def configured():
    if not get_settings().acquisition_human_recovery_enabled:
        return False
    try:
        encrypt_secret("readiness")
    except Exception:
        return False
    return True


def _latest(session, row):
    challenge = session.scalar(
        select(AcquisitionChallenge)
        .where(
            AcquisitionChallenge.job_id == row.id,
            AcquisitionChallenge.channel_profile_id == row.channel_profile_id,
        )
        .order_by(AcquisitionChallenge.generation.desc())
        .limit(1)
    )
    if challenge is None:
        raise jobs.JobNotFound("No verification request exists for this download")
    return challenge


def _erase(session, challenge_id):
    session.execute(
        update(AcquisitionBrowserCommand)
        .where(
            AcquisitionBrowserCommand.challenge_id == challenge_id,
        )
        .values(payload=None, result=None, state="expired")
    )


def close_challenges(session, row):
    for challenge in session.scalars(
        select(AcquisitionChallenge).where(
            AcquisitionChallenge.job_id == row.id,
            AcquisitionChallenge.state != "closed",
        )
    ):
        challenge.state = "closed"
        _erase(session, challenge.id)


def begin(session, row):
    """Called within the fenced fail transaction; receipt remains acquiring."""
    close_challenges(session, row)
    session.add(
        AcquisitionChallenge(
            job_id=row.id,
            channel_profile_id=row.channel_profile_id,
            generation=row.generation,
            expires_at=jobs._now() + timedelta(seconds=WAIT_SECONDS),
        )
    )


def expire_wait(channel, identity):
    with session_scope() as session:
        row = jobs._job(session, channel, identity)
        if row.status != "waiting_human":
            return
        challenge = _latest(session, row)
        if jobs._utc(challenge.expires_at) > jobs._now():
            return
        row = jobs._change(
            session,
            channel,
            identity,
            jobs.AcquisitionJob.status == "waiting_human",
            jobs.AcquisitionJob.generation == row.generation,
            status="failed",
            phase="failed",
            failure_code="human_verification_expired",
        )
        close_challenges(session, row)
        jobs._close_receipt(session, row, status="failed", code="human_verification_expired")
        jobs._event(session, row, "verification_expired", "system")


def view(challenge):
    return {
        key: getattr(challenge, key)
        for key in (
            "id",
            "job_id",
            "channel_profile_id",
            "generation",
            "state",
            "expires_at",
            "notification_state",
            "code",
        )
    }


def get_challenge(channel, identity):
    expire_wait(channel, identity)
    with session_scope() as session:
        row = jobs._job(session, channel, identity)
        challenge = _latest(session, row)
        if row.status not in {"waiting_human", "leased"} and challenge.state != "closed":
            close_challenges(session, row)
        result = view(challenge)
        from katcha.services.acquisition_notifications import notification_configuration_error

        if challenge.notification_state in {"pending", "not_configured"}:
            reason = notification_configuration_error(get_settings(), channel)
            if reason:
                result["notification_state"] = "not_configured"
                result["notification_issue"] = reason
        return result


def _locked(session, channel, identity, *, worker=None, message=None, challenge_id=None):
    row = jobs._job(session, channel, identity)
    # Lock the job before its challenge, matching cancellation/failure lock order.
    # This also serializes simultaneous browser tabs on SQLite, where FOR UPDATE
    # is ignored. Preserve the progress timestamp during mailbox-only operations.
    session.execute(
        update(jobs.AcquisitionJob)
        .where(
            jobs.AcquisitionJob.id == identity,
            jobs.AcquisitionJob.channel_profile_id == channel,
        )
        .values(updated_at=jobs.AcquisitionJob.updated_at)
    )
    session.refresh(row)
    challenge = _latest(session, row)
    session.refresh(challenge)
    if challenge_id is not None and challenge.id != challenge_id:
        raise jobs.JobConflict("This verification request was replaced; refresh the download")
    if worker is not None and (
        row.worker_principal != worker
        or row.claim_id != message.claim_id
        or challenge.generation != message.generation
    ):
        raise jobs.JobConflict("Verification belongs to a different worker execution")
    if (
        row.status != "waiting_human"
        or row.generation != challenge.generation
        or challenge.state not in {"waiting", "retry_requested"}
        or jobs._utc(challenge.expires_at) <= jobs._now()
    ):
        raise jobs.JobConflict("Verification is no longer active; refresh the download")
    receipt = jobs._receipt(session, channel, row.receipt_id)
    if receipt.status != "acquiring":
        raise jobs.JobConflict("Download permission is no longer active")
    permission = jobs._permission(session, receipt)
    return row, challenge, receipt, permission


def _command_view(command):
    active = jobs._utc(command.expires_at) > jobs._now()
    return {
        "id": command.id,
        "state": command.state if active else "expired",
        "expires_at": command.expires_at,
        "result": json.loads(decrypt_secret(command.result)) if active and command.result else None,
    }


def enqueue_command(channel, identity, message: RecoveryAction, actor):
    expire_wait(channel, identity)
    # Source navigation is deliberately not client-selected.
    raw = dict(message.action)
    if raw.get("action") == "open":
        raw["url"] = "https://source.invalid/"
    action = BrowserAction.model_validate(raw)
    if action.action == "close":
        raise jobs.JobConflict("Use Cancel download to stop this recovery")
    serialized = action.model_dump_json()
    # Keyed encryption rather than raw SHA prevents offline guessing of passwords.
    import hmac

    fingerprint = hmac.new(
        get_settings().credential_encryption_key.encode(), serialized.encode(), hashlib.sha256
    ).hexdigest()
    with session_scope() as session:
        row, challenge, _, _ = _locked(
            session, channel, identity, challenge_id=message.challenge_id
        )
        existing = session.get(AcquisitionBrowserCommand, message.id)
        if existing:
            if existing.challenge_id != challenge.id or existing.request_hash != fingerprint:
                raise jobs.JobConflict("This action identity already describes a different request")
            return _command_view(existing)
        if challenge.state != "waiting":
            raise jobs.JobConflict("Access is being checked; wait for the download result")
        session.execute(
            update(AcquisitionBrowserCommand)
            .where(
                AcquisitionBrowserCommand.challenge_id == challenge.id,
                AcquisitionBrowserCommand.expires_at <= jobs._now(),
            )
            .values(state="expired", payload=None, result=None)
        )
        active = session.scalar(
            select(AcquisitionBrowserCommand.id)
            .where(
                AcquisitionBrowserCommand.challenge_id == challenge.id,
                AcquisitionBrowserCommand.state.in_(["queued", "claimed"]),
            )
            .limit(1)
        )
        if active:
            raise jobs.JobConflict("A browser action is still pending; refresh its result first")
        count = session.scalar(
            select(func.count())
            .select_from(AcquisitionBrowserCommand)
            .where(
                AcquisitionBrowserCommand.challenge_id == challenge.id,
            )
        )
        if count >= 512:
            raise jobs.JobConflict(
                "Verification action limit reached; cancel or use another source"
            )
        command = AcquisitionBrowserCommand(
            id=message.id,
            challenge_id=challenge.id,
            request_hash=fingerprint,
            payload=encrypt_secret(serialized),
            expires_at=min(
                jobs._utc(challenge.expires_at), jobs._now() + timedelta(seconds=COMMAND_SECONDS)
            ),
        )
        session.add(command)
        session.flush()
        return _command_view(command)


def get_command(channel, identity, command_id):
    expire_wait(channel, identity)
    with session_scope() as session:
        row = jobs._job(session, channel, identity)
        challenge = _latest(session, row)
        command = session.get(AcquisitionBrowserCommand, command_id)
        if command is None or command.challenge_id != challenge.id:
            raise jobs.JobNotFound("Browser action not found for this download")
        if jobs._utc(command.expires_at) <= jobs._now() or challenge.state == "closed":
            command.state, command.payload, command.result = "expired", None, None
        return _command_view(command)


def poll(channel, identity, worker, message):
    expire_wait(channel, identity)
    with session_scope() as session:
        row = jobs._job(session, channel, identity)
        challenge = _latest(session, row)
        if (
            row.worker_principal != worker
            or row.claim_id != message.claim_id
            or challenge.generation != message.generation
        ):
            raise jobs.JobConflict("Verification belongs to a different worker execution")
        if row.status != "waiting_human" or challenge.state == "closed":
            return {"state": "closed", "command": None}
        row, challenge, receipt, permission = _locked(
            session, channel, identity, worker=worker, message=message
        )
        command = session.scalar(
            select(AcquisitionBrowserCommand)
            .where(
                AcquisitionBrowserCommand.challenge_id == challenge.id,
                AcquisitionBrowserCommand.state == "queued",
                AcquisitionBrowserCommand.expires_at > jobs._now(),
            )
            .order_by(AcquisitionBrowserCommand.created_at)
            .limit(1)
        )
        action = None
        if command and challenge.state == "waiting":
            payload = json.loads(decrypt_secret(command.payload))
            if payload["action"] == "open":
                payload["url"] = receipt.fetch_url_if_allowed
            action = {"id": str(command.id), "action": payload}
            command.state, command.payload = "claimed", None
        return {
            "state": challenge.state,
            "expires_at": challenge.expires_at,
            "command": action,
            "fetch_url": receipt.fetch_url_if_allowed,
            "permission": permission.model_dump(mode="json"),
            "code": challenge.code,
        }


def complete_command(channel, identity, worker, message):
    # Bound screenshot/page output before encryption or DB allocation.
    encoded = json.dumps(message.result, separators=(",", ":"), allow_nan=False)
    if len(encoded.encode()) > 3_000_000:
        raise jobs.JobConflict("Browser result exceeds the response limit")
    expire_wait(channel, identity)
    with session_scope() as session:
        _, challenge, _, _ = _locked(session, channel, identity, worker=worker, message=message)
        command = session.get(AcquisitionBrowserCommand, message.command_id)
        if (
            command is None
            or command.challenge_id != challenge.id
            or jobs._utc(command.expires_at) <= jobs._now()
        ):
            raise jobs.JobConflict("Browser action expired; refresh the view before continuing")
        if command.state == "completed" and command.result:
            if json.loads(decrypt_secret(command.result)) == message.result:
                return {"accepted": True}
            raise jobs.JobConflict("Browser action already has a different result")
        if command.state != "claimed":
            raise jobs.JobConflict("Browser action has not been claimed")
        command.result, command.state = encrypt_secret(encoded), "completed"
        challenge.code = None
        return {"accepted": True}


def request_retry(channel, identity, message, actor):
    expire_wait(channel, identity)
    with session_scope() as session:
        row, challenge, _, _ = _locked(
            session, channel, identity, challenge_id=message.challenge_id
        )
        pending = session.scalar(
            select(AcquisitionBrowserCommand.id)
            .where(
                AcquisitionBrowserCommand.challenge_id == challenge.id,
                AcquisitionBrowserCommand.state.in_(["queued", "claimed"]),
                AcquisitionBrowserCommand.expires_at > jobs._now(),
            )
            .limit(1)
        )
        if pending:
            raise jobs.JobConflict("Wait for the browser action before checking access")
        if challenge.state == "waiting":
            challenge.state, challenge.code = "retry_requested", None
            jobs._event(session, row, "verification_requested", actor)
        return view(challenge)


def unavailable(channel, identity, worker, message):
    expire_wait(channel, identity)
    with session_scope() as session:
        _, challenge, _, _ = _locked(session, channel, identity, worker=worker, message=message)
        challenge.state, challenge.code = "waiting", message.code
        return view(challenge)


def resume(channel, identity, worker, message):
    expire_wait(channel, identity)
    with session_scope() as session:
        row = jobs._job(session, channel, identity)
        challenge = _latest(session, row)
        # A lost resume response cannot create another generation or source attempt.
        if (
            challenge.generation == message.generation
            and challenge.state == "retrying"
            and row.status == "leased"
            and row.generation == message.generation + 1
            and row.claim_id == message.claim_id
            and row.worker_principal == worker
            and jobs._utc(row.lease_expires_at) > jobs._now()
        ):
            receipt = jobs._receipt(session, channel, row.receipt_id)
            if receipt.status != "acquiring":
                raise jobs.JobConflict("Download receipt is no longer active")
            return row, receipt.fetch_url_if_allowed, jobs._permission(session, receipt)
        row, challenge, receipt, permission = _locked(
            session, channel, identity, worker=worker, message=message
        )
        if challenge.state != "retry_requested":
            raise jobs.JobConflict("The operator has not requested an access check")
        row = jobs._change(
            session,
            channel,
            identity,
            jobs.AcquisitionJob.status == "waiting_human",
            jobs.AcquisitionJob.generation == message.generation,
            status="leased",
            phase="acquiring",
            generation=message.generation + 1,
            bytes_received=0,
            heartbeat_sequence=0,
            failure_code=None,
            lease_expires_at=jobs._now() + timedelta(seconds=jobs.LEASE_SECONDS),
            last_heartbeat_at=jobs._now(),
        )
        challenge = session.get(AcquisitionChallenge, challenge.id)
        challenge.state = "retrying"
        _erase(session, challenge.id)
        jobs._event(session, row, "verification_retry_started", worker)
        return row, receipt.fetch_url_if_allowed, permission


def clean_expired_mailboxes():
    """Erase expired secrets even when no operator revisits the recovery page."""
    if not get_settings().acquisition_human_recovery_enabled:
        return
    with session_scope() as session:
        session.execute(
            update(AcquisitionBrowserCommand)
            .where(
                AcquisitionBrowserCommand.expires_at <= jobs._now(),
                (
                    AcquisitionBrowserCommand.payload.is_not(None)
                    | AcquisitionBrowserCommand.result.is_not(None)
                ),
            )
            .values(payload=None, result=None, state="expired")
        )
